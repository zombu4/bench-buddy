# I/O layer review findings

Independent cold review against `IO-DISCIPLINE.md`. Ranked. Several of these can
leave the user's physical instrument in a harmful state, so 1-11 are the ones
that gate running the app against real hardware.

## Design change that removes a whole class of these

**Replace `TRIG:COUN INF` in the idle keepalive with a FINITE, RENEWED count.**

Finding 9 is unfixable in software: if the process dies, an `INF` acquisition
keeps running instrument-side with no drainer and reading memory (1000) fills in
seconds. No `atexit`, `excepthook` or signal handler survives a hard kill, and
trigger state is instrument-global, not per-session.

A finite count is self-limiting — a deadman the instrument itself enforces:

- size the count for roughly 2 s of acquisition at the current measured rate,
  clamped to **[50, 500]** — always well under the 1000-reading memory
- the drain loop renews it when the remaining count falls below half
- if the app dies, or a caller holds `_gate` for 3 s (`passthrough`) or 15 s
  (`capture_screen`), the acquisition simply expires. No overflow, ever.

This also defuses findings 7 and 8, which are otherwise structural. Keep the
drain, keep the guard, but stop relying on them for safety.

## CONFIRMED — can harm the instrument

**1. Neither acquisition loop's cleanup is in `finally`.**
`instrument.py:1475-1529` (`_stream_loop`), `:1290-1338` (`_beat_loop`). Both
catch only `ScpiError`; the ABOR cleanup sits after the `while` at function-body
level. `parse_block_floats` (`scpi.py:480-486`) raises **`ValueError`** on a
malformed block — called at `:1490` and `:1262`. One bad `R?` kills the thread
with `TRIG:COUN INF` running and undrained; `_streaming` stays `True` so the
heartbeat stands aside permanently. At NPLC 0.02 memory fills in ~1.4 s →
`-365`, dead panel, UI still says "Running". Put cleanup in `finally` on both.

**2. `close()` continues after `stop_heartbeat()`/`stop_stream()` raise.**
`instrument.py:343-357`. Join timeouts raise at `:1162`/`:1402`, are swallowed at
`:345`/`:349`, and shutdown proceeds to `SYST:LOC`. The still-live daemon thread
then sends `ABOR` (`:1333-1337`) or `DATA:POIN?` after it, re-asserting Remote —
and in the heartbeat case leaving the meter Remote + ABORted + idle, the frozen
panel from rule 2. `SYST:LOC` needs to wait until every worker thread is
confirmed stopped; if one will not stop, the honest move is to say so rather than
hand back a broken state.

**3. "Return to Local" is undone by the app itself, three times over.**
- `instrument.py:416` — `return_to_local()` calls `publish_state()` immediately
  after `release()`: ~33 SCPI queries, Remote re-asserted within milliseconds.
- `bridge.py:478` — the slot then calls `_emit_state()`, another 33 queries.
- `bridge.py:96`/`:34` — `_state_timer` keeps firing every 5 s and is never
  stopped, so Remote returns permanently regardless.

The heartbeat is deliberately left stopped (`:412`), so this lands the meter in
Remote-and-idle — exactly the reported fault — while the UI reports success.
`SYST:LOC` must be the last command on the link: stop the state timer, stop all
polling, then send it, then touch nothing.

**4. `_restore_trigger_locked` clears the saved trigger before the writes succeed.**
`instrument.py:1430`. `_trig_saved` is cleared first, then `ABOR` + restores.
`abort_stream` (`:1451-1457`) is called precisely when the link just failed, so
the `ABOR` raising is the expected case. `_trig_saved` is then gone, the meter
sits at `IMM/INF/1`, and the next `_start_idle_acquisition_locked` (`:1208`)
captures `("IMM","INF",1)` as the user's setup — which shutdown faithfully
restores. Clear the saved value only after the restore writes succeed.

**5. Config/trigger/math writes `ABOR` first and validate second, no `try/finally`.**
`instrument.py:828-836`, `:938-980`, `:1011-1018`, `:1093-1115`.
`_pause_for_change()` sends `ABOR`, then `_apply_config_field` raises on
validation failure, so `_resume_after_change()` never runs. Reachable: streaming,
switch to 4W with a queued `azero` write — `specs.py:150` has `azero=False` for
FRES so `:858` raises after the ABOR. Stream loop then polls `DATA:POIN?` → 0
forever, heartbeat stands aside, instrument measures nothing, UI shows "Running".
Same via impedance/band/aperture/nplc and `set_trigger` validation; `single()`
`:1110` skips its restore on `ScpiTimeout`. Wrap every one in `try/finally`.

**6. `query_block` raises on a bad header without resetting the link.**
`scpi.py:422-435`. Four paths raise while the rest of the response is in flight;
`query()` clears `_buf` (`:401`) but never drains the socket, so up to 277 KB of
a screen frame is read as the next query's answer. Every timeout path correctly
calls `reconnect()`; these four do not. This desync is the input to finding 1.
SPEC §3 requires a rebuild on any framing loss — apply it here too.

**7. Four long operations hold `_gate` while the keepalive acquisition free-runs undrained.**
`_read_state_online` (~33 ops, ≥0.8 s), `system_info` (~30 ops, ≥0.75 s),
`capture_screen` (`SCREEN_TIMEOUT = 15.0`), `passthrough`
(`PASSTHROUGH_TIMEOUT = 3.0`). Only `single`, `selftest` and `_pause_for_change`
call `_stop_idle_acquisition_locked` first. Memory holds 1000; at NPLC 0.02 that
is 1.39 s — so a stalled console command or screen grab **guarantees** `-365`.
The "drain again above 200" guard (`:1326`) does nothing while another caller
owns `_gate`, so it is not structural. Make every gate-holding operation stop
idle acquisition first, uniformly.

**8. `ScpiTimeout` backoff in the heartbeat leaves acquisition running up to 5 s.**
`instrument.py:1305-1314`. No `ABOR`, `_idle_initiated` stays `True`, sleeps
`min(1.0*failures, 5.0)`. At NPLC 0.02 that is ~3600 undrained readings.

**9. Crash path has no safety net.** No `atexit`, `excepthook`, `__del__` or
signal handler anywhere. PySide6 kills the process on an unhandled slot
exception, so `closeEvent` never runs, leaving `TRIG:COUN INF` acquiring with no
drainer. Add the handlers as defence in depth, but the real fix is the finite
renewed count above.

**10. `_idle_backlog` is never reset to zero.**
`instrument.py:1254` returns early on `available <= 0` without touching it; only
assigned at `:1259`. After a beat draining ≥200, the next empty beat leaves the
stale value and `:1326` `continue`s with no sleep. Two shapes: a hot
`DATA:POIN?` loop at the full 40 ops/s ceiling for an integration period
(the sustained-polling pattern that degraded the LAN stack), or — when the user
selects BUS/EXT trigger so `_beat_safe` is `False` and `_beat_once` returns at
`:1243` — **a bare `continue` spinning at 100% of one core forever**.

**11. Reconnect backoff never arms on read timeouts, only TCP connect failures.**
`scpi.py:266-281`. `_arm_backoff()` is called only from `connect()`'s
`except OSError`. A wedged-but-accepting instrument — precisely the degraded
state we hit — takes the read-timeout path, `reconnect()` succeeds, `_attempts`
stays 0, and the next timeout rebuilds immediately. `write()` also calls
`_note_healthy()` unconditionally at `:391` on a `sendall` that succeeds
regardless. Result: one socket close/open per timeout with no spacing — the
documented mechanism that degraded this instrument. Socket opens are also
ungated by the rate limiter.

## CONFIRMED — lower severity

**12. `LIMITER.acquire()` sleeps while holding the link lock.** `scpi.py:291-307`.
A throttled thread blocks every other user of the link. No deadlock and token
accounting is correct, but shutdown's `ABOR`/`SYST:LOC` queue behind whatever is
throttling, and the drain loop can lose repeatedly to a 33-op state read.
Give shutdown a priority path.

**13. `_on_local`'s failure branch is unreachable.** `instrument.py:400-401`
returns `local: True` in both branches, so `main.py:685` is always true and the
"press [Local]" fallback at `:691-696` can never run.

## SUSPECTED

**14. `_paused` is a non-atomic counter** mutated outside any lock
(`instrument.py:197`/`:203`), incremented before acquiring `_gate`. An
interrupted acquire leaves it >0 and the heartbeat never beats again.

**15. `check_allowed` matches only short mnemonics** (`scpi.py:164-171`), so
legal long forms (`SAMPle:SOURce?`) pass the guard. Latent, not live.

**16. The user's trigger count/samples are silently overwritten by the keepalive.**
`_heartbeat_safety_locked` (`:1179-1197`) checks only `TRIG:SOUR`. Set Count=10
while idle and the UI shows `INF`/`1` within 5 s. `_trig_saved` is correct so
release restores properly, but the user sees their setting revert unexplained.

**17. `reset()` does not clear `_trig_saved`** (`:1857-1873`), so shutdown writes
pre-`*RST` values over the power-on setup.

## Confirmed clean — do not churn

Removal fallout (no `view` socket, capture thread or `/api/screen` reference
survives; `capture_screen` pause is exception-safe via `with`; `mirror.py` never
presents the frame as live). Overload sentinel handling at every publication
point — `9.91E37` cannot reach the UI as a number. Single-deadline `query_block`.
SPEC §2.2 never-send gating, including `FRES:ZERO:AUTO?` and `CAP:RES?`.
Rate-limiter coverage of SCPI operations. Qt threading is substantially clean.

Two cosmetic leftovers: `bridge.py:145`,`:595` and `main.py:857` still say "both
sockets"; orphan `app/__pycache__/server.cpython-314.pyc` from the deleted
FastAPI layer.

Two Qt notes worth addressing: `bridge.py:93` uses `Qt.DirectConnection` so
`_on_publish` runs on the heartbeat/stream thread, not the worker — the
docstrings at `:12-14`/`:44-49` describe marshalling that is not happening, and
it works only by luck of `AutoConnection` at the second hop. And `main.py:859`
blocks the GUI thread up to 30 s during `closeEvent` with a re-entrant
`processEvents()` at `:858` that can dispatch a queued user action after
`_closing` is set.
