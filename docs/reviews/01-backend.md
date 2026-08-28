# Backend review findings

An independent review of this code against SPEC.md, read cold, with live
read-only probing of the instrument. Ranked. CONFIRMED means traced or
reproduced.

## CONFIRMED

**1. `/api/scpi` timeout silently kills the run; UI still shows "Running"**
`instrument.py:999-1007`, `server.py:313-319`. `passthrough()` catches
`ScpiTimeout`, sets `_streaming=False` and `_stream_stop.set()`, but never sends
`ABOR`, never restores `_trig_saved`, and `api_scpi` is the one mutating endpoint
that does not call `push_state()`. Any unsupported query typed into the console
while streaming freezes the readout forever with `streaming:true` cached client
side.

**2. `_trig_saved` clobbered on abnormal stop, destroying the user's trigger setup**
`instrument.py:656` vs `674-690`, `717-726`, `1002-1006`. `start_stream()`
unconditionally captures `_trig_saved`. Both abnormal-stop paths clear
`_streaming` without consuming it. Run → passthrough timeout → Run again captures
the streaming config (IMM/INF/1) as "the user's" trigger and the real
`TRIG:SOUR BUS` / `TRIG:COUN 500` / `SAMP:COUN 10` are unrecoverable.

**3. `9.91E37` overload sentinel never recognised**
`instrument.py:742, 752, 760-768, 304-305, 638`. Verified live: `DATA:LAST?` →
`+9.91000000E+37  VDC`, `CALC:TRAN:HIST:RANG:LOW?/UPP?` → `9.91E37`.
Consequences: `_emit_periodic` compares it against limits and fires a guaranteed
false `{type:"limit", status:"fail_high"}` at the start of every limited run;
`read_state` publishes `hist_low/hist_high` as ordinary floats; `single()` returns
`9.91e37`; `_on_readings` publishes overloaded samples raw, wrecking chart
autoscale and the logged CSV.
Fix: a single `is_overload(v)` helper (`abs(v) >= 9.9e37`). Surface overload as an
explicit state/flag, never as a number. Null out `hist_low`/`hist_high` when
sentinel. Exclude overloads from limit evaluation. Mark them in `data` messages
and in the CSV so the frontend can render "OVLD" rather than plot 9.91e37.

**4. `state.error` is a one-way latch**
`instrument.py:64, 193, 719, 913`. `last_error` is never reset to `None` on
success, so one transient hiccup pins a stale error into every `/api/state` and
every `{type:"state"}` for the life of the process. Clear it on the next
successful operation.

**5. Stream loop fatal path publishes an error but never a state**
`instrument.py:717-726`. Any `ScpiTimeout`/`ScpiConnectionError` on `ctrl`, or 3
consecutive `ScpiError`s, stops the stream with only `{type:"error"}`. SPEC §4.2
requires a `{type:"state"}` after any change. `ABOR` is never sent, so the
instrument keeps running `TRIG:COUN INF` into reading memory unattended.

**6. `/api/state` response shape violates the contract**
`server.py:158-160` returns `{"ok":true,"state":{...}}`. SPEC §4 requires the
flattened State object, and `/api/system`, `/api/hist`, `/api/single`,
`/api/stats`, `/api/selftest` already spread with `**`. The frontend is coded to
the contract and will read `resp.func` as `undefined`. Flatten it.

**7. `passthrough` decides query-vs-write by substring `"?"`**
`instrument.py:986`. `DISP:TEXT "ready?"` is a legal command misclassified as a
query; it waits 3 s, times out, and tears down the ctrl link (and per #1 kills any
run). Test whether the command's mnemonic ends in `?`, ignoring quoted strings.

**8. Non-query passthrough never checks the error queue**
`instrument.py:995-998`. A rejected command returns `{"response":"","error":null}`,
indistinguishable from success. Drain `SYST:ERR?` after a non-query passthrough
and report it.

**9. Log overflow invisible; `log_csv()` materialises ~150 MB**
`instrument.py:35, 733-741, 857-860, 844-867`. At `MAX_LOG_POINTS=2_000_000` the
buffer stops appending but `_logging` stays true and `log_count` pins; the flag
appears only as a comment row in the CSV. Surface overflow in the State object and
over the WebSocket. Stream the CSV (generator / `StreamingResponse`) instead of
building one giant string.

**10. `stop_screen`/`stop_stream` ignore a failed `join()`**
`instrument.py:674-679, 893-899`. Neither checks `is_alive()` after the timeout,
so a second thread can be spawned while the first is still blocked, with two
threads issuing `R?` and splitting readings. Check liveness and refuse to start a
duplicate.

**11. WebSocket dies on one malformed client frame**
`server.py:449-451`. A `JSONDecodeError` sends `{type:"error"}` then falls out of
the loop and closes. §4.2 defines `error` as in-band; keep the connection open.

**12. `query_block` multiplies its own timeout by 4**
`scpi.py:262-283`. `tmo` is passed to each of four `_read_exact` calls, so worst
case is 4×. Breaks SPEC §4's "short (3 s)" passthrough guarantee — a block-
returning query can hold the ctrl link and `_gate` for 12 s. Apply one deadline
across the whole command.

**13. Unbounded, unordered WebSocket fan-out**
`server.py:75-82, 64-73`. `publish_threadsafe` fires a `_broadcast` task per
message with no backpressure and never inspects the Future. At ~200 msg/s a slow
client accumulates tasks without bound, and concurrent `_broadcast` tasks can
interleave at the await point, delivering `data` out of timestamp order.
Serialise sends per connection (a queue per client, drop-oldest when behind).

**14. Server refuses to start if the instrument is unreachable; `connected` is dead code**
`server.py:498-509`, `instrument.py:188-192`. `main()` calls `build()` →
`device.open()` before `uvicorn.run()`, so a powered-down DMM means a traceback
and no HTTP server. `read_state()` raises rather than degrading, so
`connected: bool` (§4.1) is always true. Start the server regardless, report
`connected:false` with the real error, and reconnect in the background.

**15. Latent misresolution in `resolve_sense_func`**
`specs.py:246-248`. The `startswith` fallback tests `"VOLT"` before `"VOLT:AC"`,
so `VOLTAGE:AC` would resolve to `VOLT:DC`. Not currently reachable (this unit
returns the short quoted form) but every downstream node would be composed for the
wrong function. Match longest-prefix-first.

**16. Minor contract drift**
- `nplc_options` is `[]` for non-NPLC functions (`instrument.py:203`); §4.1 shows
  it unconditionally.
- No handler for FastAPI `RequestValidationError`, so a bad body returns 422
  `{"detail":[...]}` not `{ok:false,error:"..."}`.
- `push_state()` missing after `/api/stats/clear`, `/api/hist/clear`,
  `/api/lock`, `/api/screen`.

## SUSPECTED

**17. `*RST` probably reverts the hardcopy format and permanently breaks the mirror**
`instrument.py:92, 872-876`. `_screen_format_set` is a sticky one-shot. If `*RST`
resets the format to PNG, every grab thereafter fails `decode_bmp` and after 3
failures `_screen_loop` exits for good, while `/api/screen.png` serves the last
cached frame forever. Clear `_screen_format_set` in `reset()` regardless, and
re-assert the format if a decode fails.

**18. Stale screen frames served indefinitely with no staleness signal**
`instrument.py:922-928`, `server.py:385-395`. After the capture thread dies the
endpoint keeps returning the last frame. Put `screen_running` and frame age in the
State object.

**19. Reading-memory overflow unmonitored**
`instrument.py:188-260, 695-704`. `read_state()` holds `_gate` across ~34 round
trips (~45 ms), buffering ~35 readings at 718 rdg/s against the 34461A's
1000-reading memory. The stream loop never checks `SYST:ERR?`/`STAT:QUES:COND?`,
so `-365 Reading memory overflow` would silently drop samples. Detect and surface.

**20. `_rate_events` trimmed only inside `rate_hz()`**
`instrument.py:75, 730-731, 770-776`. Grows one entry per `R?` block and is pruned
only when state is read. Trim on append.

**21. start/stop race on the stream thread**
`instrument.py:652-672, 674-679`. `_stream_stop.clear()` and `thread.start()`
happen outside `_gate`. A concurrent `stop_stream` makes
`POST /api/stream {run:true}` return `streaming:false` with no error. Move the
whole start sequence inside the gate.

**22. `enumerate_ranges` truncates silently past 32 decades**
`specs.py:262-267`. Exits the loop and appends MAX regardless, producing a short
wrong list instead of the SPEC §0-mandated "stop and report". Raise instead.

## Confirmed clean — do not churn

Forbidden-command gating (every node composition is gated on a `FunctionSpec`
boolean; `CONT`/`DIOD` have `sense=None` with guarded consumers; `check_allowed`
on all internal paths; only `passthrough` bypasses, as required). Lock ordering
(strictly `_gate` → `ScpiLink.lock`, no inversion, no deadlock). Reset during a
half-read block (the RLock is held across the whole exchange). BMP decode
(verified against live bytes: stride recomputed not trusted, `top_down` handled,
5→8 bit expansion exact at both endpoints, truncation raises a clean
`ScreenDecodeError`). No stubs or bare excepts. `TRIG:COUN INF` round-tripping.
`DATA:LAST?` unit-suffix stripping. `R?` block framing.

`FREQ:VOLT:RANG:AUTO?` is extrapolated rather than spec-verified but was tested
live and answers `1` in 3.9 ms — it is safe.
