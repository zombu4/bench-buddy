# VXI-11 transport review findings

Independent cold review of the hand-rolled VXI-11 transport against RFC 1057/1833
and VXI-11.3, with live measurement on 192.0.2.50. Line numbers in `scpi.py` are
stable; `instrument.py` is named by function because it was being edited
concurrently.

## The architectural correction behind findings 3, 5 and 6

**A VXI-11 read timeout is an in-band result, not a poisoned stream.** The raw
socket transport had to assume that a timeout meant the byte stream was
desynchronised, because it is a stream — hence "any read timeout closes and
rebuilds the socket". That assumption was carried into VXI-11 unchanged, and it
is wrong there: `device_read` returning error 15 means the RPC *completed*, the
channel is in sync, and nothing is in flight.

Tearing the session down anyway sends DESTROY_LINK on the sole link, which makes
the instrument device-clear itself. So the deadman — the whole reason for
adopting VXI-11 — now fires on ordinary error recovery.

Fix the taxonomy transport-by-transport: on VXI-11, a read timeout must be
reported to the caller **without** destroying the link. Reserve teardown for
genuine channel corruption (a bad RPC reply, an xid mismatch, a truncated
fragment), which is what `_framing_reset` is properly for.

## CONFIRMED

**1. The single deadline is not enforced inside one RPC; timeouts multiply per TCP segment.**
`scpi.py:444-451, 470-490`. `_RpcChannel.call` calls `settimeout(timeout)` once,
then `_recv_exact` loops on `recv()` — each syscall gets the full timeout afresh.
Worst case is `timeout x N_segments`, unbounded. Measured: one 277 KB screen grab
is 5 `device_read` RPCs of 65536 B, each spanning many segments, so a nominal
15 s `SCREEN_TIMEOUT` can block for minutes while holding `ScpiLink.lock` and the
instrument `_gate` — wedging the UI and the streaming drain. `_read_exact`'s
deadline is only checked *between* `_recv_chunk` calls and cannot interrupt it.
**Divergence:** `RawSocketTransport.recv` (313-323) honours the deadline to
within one timeout period. The `query_block` single-deadline contract hardened by
a previous pass holds on socket and not on VXI-11. Thread one absolute deadline
through `_recv_exact` and recompute the per-syscall timeout from it.

**2. VXI-11 write failures bypass the reconnect backoff, producing unbounded link churn.**
`scpi.py:1022-1045` + `631-659`. On VXI-11 a `write()` is a full DEVICE_WRITE
round trip (socket timeout 12 s). `_send` catches the failure, calls `close()` and
raises `ScpiConnectionError`; `close()` (929-939) deliberately does not arm the
backoff, and only `_timeout_reset` and a failed `connect()` do. Against an
instrument that accepts TCP and CREATE_LINK but stalls on DEVICE_WRITE — the
wedged-but-accepting state rule 3 exists for — this is an unbounded
destroy/create cycle at ~1 per 12 s with no spacing, gated only by the rate
limiter. That is the open/close churn IO-DISCIPLINE.md blames for degrading this
unit, and on VXI-11 every cycle also device-clears the meter. Arm the backoff on
write-path teardowns too.
**Divergence:** unreachable on raw socket, where `sendall` does not time out
against a wedged-but-accepting peer.

**3. A read timeout device-clears the instrument.**
`scpi.py:661-696` + `605-622`. `recv` maps error 15 to `TransportTimeout`;
`_recv_chunk` (1050-1051) routes it to `_timeout_reset`, which rebuilds the
session. Measured on the hardware (`TRIG:COUN 200; INIT`, NPLC 10): `DATA:POIN?`
went 2 -> 8 -> 14, the sole link was destroyed, and on a fresh link 3 s later the
count was frozen at 8 — acquisition aborted, memory cleared. Two consequences
beyond the abort: (a) the deadman fires on ordinary recovery; (b) the readings the
meter free-ran while the link was down are delivered by the next `R?` as if live
and refresh `last_readings_at`, so `STREAM_RESTART_AFTER` does not fire promptly
— a small-scale version of the stale-readings fault VXI-11 was adopted to fix.
See the architectural note above.

**4. A stalled console write leaves a zombie "Running".** `instrument.py`,
`passthrough`. It catches `ScpiTimeout` (sets `stop_run`) then `ScpiError` (does
not). Per finding 2, a stalled DEVICE_WRITE raises `ScpiConnectionError` — an
`ScpiError` but not a `ScpiTimeout` — so the teardown device-clears the
acquisition while `_streaming` stays True and the UI still says "Running". Self-
heals only after `STREAM_RESTART_AFTER` (10 s). Fixing finding 2's error taxonomy
may resolve this; confirm rather than assume.

**5. DESTROY_LINK on every teardown lengthens the churn hot path.**
`scpi.py:605-622`. `close()` issues a DESTROY_LINK RPC with a 2 s timeout even
when the teardown was *caused* by a stalled instrument, and per finding 1 that
2 s re-arms per syscall — all with `ScpiLink.lock` held. Raw-socket `close()` is
immediate. It is also unnecessary for the deadman: closing the socket reaches the
same instrument state, as the module docstring itself says. Skip or hard-bound it
when the teardown is a failure path.

**6. A zero-length successful read is reported as a failure.**
`scpi.py:692-695`. `device_read` returning error 0 with no data raises
`TransportTimeout`, which arms the backoff and destroys the link with finding 3's
device clear. The comment says "treat it as nothing-yet so the caller's deadline
decides", but `_recv_chunk` never retries on `TransportTimeout`. Code and stated
intent disagree — make it a genuine no-data-yet that the caller's deadline
governs.

**7. `_VXI_READ_CHUNK` is inert and its comment is false.**
`scpi.py:380-383`. Measured: this unit caps a `device_read` reply at 65536 bytes
regardless of `requestSize`, so the 277 KB frame took five RPCs
(65536x4 + 15359) — exactly the "five round trips" the comment claims 262144
avoids. No behavioural defect; correct the constant and the comment.

**8. `_open_transport` discards the second failure.** `scpi.py:882-883`.
`if first_error is None` means that under `transport="auto"` with both transports
failing, the raised error names only the VXI-11 failure — so a genuinely
unreachable instrument is reported as a VXI-11 problem, inviting the user to
force `socket` and fail again. Report both.

## SUSPECTED

**9. No bound on fragment length or count.** `scpi.py:483-485`.
`length = header & 0x7FFFFFFF` goes straight to `_recv_exact`, so a corrupt
4-byte header makes the client accumulate up to 2 GB before a per-syscall timeout
stops it. A sanity cap is one line.

**10. Bytes transferred alongside error 15 are discarded.** `scpi.py:685-691`.
VXI-11 permits `device_read` to return error 15 *with* the bytes already
transferred; `recv` raises before calling `reader.opaque()`. Harmless as wired
today, but becomes silent truncation the moment anyone passes `recv` an
io_timeout shorter than the caller's deadline.

**11. `query`/`query_block` clear stale bytes without rebuilding.**
`scpi.py:1130-1132, 1144-1145`. The comment correctly names stale bytes as
evidence of a prior desync, yet unlike `_framing_reset` it does not rebuild, and
the rest of the stale response is still on the wire. Identical on both
transports; pre-existing, not a VXI-11 regression.

**12. RPC-layer status codes are not decoded.** `scpi.py:498-499, 503-504`.
`reject_stat` is undecoded, so an RPC version or program mismatch reports only
"the instrument rejected the RPC call"; `accept_stat` is reported as a bare
integer rather than PROG_UNAVAIL / PROC_UNAVAIL / GARBAGE_ARGS.

## Confirmed clean — do not churn

**The deadman holds.** The only extra TCP connection is the portmapper probe
(`_lookup_core_port`, 539-560), which creates no VXI-11 link and closes in a
`finally`; `_core_port` caching means it is not reopened on a rebuild. Every
error path in `Vxi11Transport.open` closes the channel before propagating
(599-601); `reconnect()` always closes before connecting; `connect()` is a no-op
when a transport exists; `_open_transport` closes a failed candidate before
trying the next. No path opens a second link or keeps a stale one across a
reconnect.

**ONC RPC / XDR encoding is correct**, checked field by field: record marking
(0x80000000 last-fragment bit, 31-bit length, multi-fragment reassembly),
big-endian throughout, 4-byte opaque/string padding on encode and decode,
AUTH_NULL cred+verf, reply parse order, portmapper GETPORT, and the
argument/reply field orders of CREATE_LINK, DEVICE_WRITE (flags bit 3 = END, set
only on the final piece), DEVICE_READ, DEVICE_CLEAR and DESTROY_LINK. xids
increment per channel, replies are matched to requests, and a mismatch is fatal
rather than resynced — the right call.

**`device_read` framing is clean, for a non-obvious reason.** `recv` ignores
`reason` and callers terminate on `\n` or the definite-length count. Measured,
this instrument returns `reason = 0` on every intermediate 65536-byte chunk and
sets END (4) only on the last — so code that terminated on REQCNT, or treated
`reason == 0` as done, would have truncated the 277 KB frame. `maxRecvSize` is
read and floored correctly (this unit advertises 0xFFFFFFFF).

**Transport selection cannot oscillate.** `_settled` is written once and never
reset; forced `"vxi11"` raises rather than degrading; a mid-session reconnect
cannot change transport; `crash_safe` reads the transport actually in force and
is False whenever the link is down — the safe direction.

**Prior hardening is preserved:** reconnect-on-timeout, the single `deadline`
threaded through `query_block`'s four `_read_exact` calls, definite-length block
parsing, `check_allowed` with node-count matching, and the 40 ops/s limiter with
its priority path all live in `ScpiLink` above the transport and are byte-
identical on both. The only divergences are the transport error taxonomy —
findings 1, 2, 3 and 6.

**Rule 6 holds on VXI-11.** DESTROY_LINK is not a SCPI command, and the device
clear it triggers leaves the meter free-running in local. Measured: destroying
the sole link with no `SYST:LOC` at all left the panel free-running at ~2.7 rdg/s
until a new link put it back into remote.
