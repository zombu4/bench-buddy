# Instrument I/O discipline — hardware safety notes

Written after wedging the physical 34461A during development. These are not
style preferences: ignoring them degraded a real instrument until it needed a
power cycle. What follows is what went wrong, what was measured afterwards, and
the rules the implementation follows as a result. Everything here was measured
against the meter rather than reasoned about, and where a measurement later
contradicted an earlier conclusion both are kept, in order, so the reasoning can
be followed.

See also `SPEC.md` for the instrument's verified command set and never-send
list, and `ARCHITECTURE.md` section 2 for the threading model these rules run
inside.

## What actually happened

Two separate problems were conflated during debugging. Both are addressed here.

**Problem 1 — the front panel "freezes" the moment anything connects.**
This is normal, documented Truevolt behaviour, not a fault. Any SCPI command
over LAN puts the instrument into **Remote** mode (the "Remote" annunciator
lights). In remote the front panel stops free-running: it holds the last
acquired reading and updates only when a measurement is actually commanded.
An app that connects and then sits idle leaves the user's bench meter looking
dead. Observed directly: the display froze on `-0.000,368 VDC`, which was the
last `READ?` of a reset sequence, and stayed there.

**Problem 2 — progressive unresponsiveness under load.**
The instrument runs Windows CE 6.0 (confirmed from its own HTTP `Server:`
header) with a limited LAN stack. Sustained ~6 fps `HCOP:SDUM:DATA?` screen
capture (277 KB per frame) on a second socket, concurrent streaming at up to
~840 readings/s, repeated `ABOR`/`INIT` cycles, and **hundreds of socket
open/close cycles** across test runs degraded it until HTTP and SCPI both went
intermittent — TCP would accept, then services would time out at random. Only a
power cycle cleared it. The measurement engine itself never failed; it returned
fresh varying values throughout. This was resource exhaustion in the LAN stack,
caused by us.

## The rules the implementation follows

### 1. Keep the instrument measuring whenever the app is connected

The app never holds a connection while leaving the trigger system idle.

- **Streaming mode** (chart running): the `INIT` + `R?` drain pattern.
  Measurements are flowing, so the front panel updates naturally.
- **Idle mode** (connected, not streaming): a low-rate heartbeat at **2–4 Hz**,
  originally specified as `READ?`. The intent was to keep the front panel live
  and the reading current without filling the 1000-reading memory, and the
  original rule ruled out `INIT` with `TRIG:COUN INF` for idle keepalive on the
  grounds that it fills reading memory and risks a `-365` overflow. Both halves
  of that prescription were then measured, and both are revisited below.
- The transition between the two never leaves a window where neither runs.

#### Measured 2026-08-27 — `READ?` does not keep this panel live, and is not what shipped

The goal of this rule is met, but the specific command it originally named is
not what shipped. The prescription was tested against the instrument before
being relied on, and it does not achieve the outcome the rule asks for.

`READ?` is `ABORt` + `INITiate` + `FETCh?`, and the `ABORt` **blanks the
displayed reading** for the whole of the following integration. The panel
repaints roughly 100 ms after a reading completes, so at NPLC 10 — 343 ms per
reading — the digits are on screen for only a sliver of each cycle.

Occupancy was sampled from an *independent* socket while the keepalive ran, so
the samples were not synchronised to it. This matters: the application's own
screen grab waits on the instrument gate and therefore always lands at the one
instant a reading ends, which flatters `READ?` badly. What a person at the bench
actually sees:

| Idle pattern | Beat rate | Digits visible | Reading memory |
|---|---|---|---|
| `READ?` + 120 ms gap | 2.01 Hz | **2/10** | empty |
| `READ?` + 250 ms gap | 1.67 Hz | 5/10 | empty |
| `READ?` + 400 ms gap | 1.35 Hz | 5/10 | empty |
| `INIT` + `R? 1` per beat | 1.92 Hz | 3/10 | empty |
| **`TRIG:COUN INF`, drained every beat** | **3.18 Hz** | **12/12** | **0 backlog** |

Only a *continuous* acquisition keeps the panel lit, because the display holds
the previous reading while the next one integrates; anything that ends and
restarts an acquisition blanks it. A `READ?` keepalive leaves the meter looking
dead in a new way while reporting fresh values to the app the whole time — the
failure mode is invisible from the application side, which is why it is worth
recording here.

**What is implemented instead:** `TRIG:SOUR IMM; TRIG:COUN INF; SAMP:COUN 1;
INIT`, drained with `DATA:POIN?` + `R?` at 3 Hz. This keeps the rule's goal
("keeps the front panel live") and its safety property ("without filling the
1000-reading memory"), and departs only from the named command:

- the `-365` overflow the prohibition exists to prevent comes from leaving a
  continuous acquisition **undrained**. Drained every beat, measured backlog was
  **0** with an empty error queue.
- the drain is guarded so the risk is structural, not hopeful: if more than 200
  readings are ever waiting it drains again immediately instead of sleeping, and
  the keepalive always `ABOR`s its own acquisition before letting go of the link.
- it costs 2 operations per beat, ~7 of the 40 per second allowed by rule 5.
- it self-heals: if anything else `ABOR`s the acquisition (a single reading, a
  `*RST`, a console command), no readings arrive for 1.5 s and it re-initiates.

#### Superseded 2026-08-27 — the count was made finite and renewed

`TRIG:COUN INF` made the drain the only thing between the instrument and a full
reading memory, and the drain lives in the application. Kill the process and the
acquisition keeps running instrument-side with nobody draining it; trigger state
is global to the instrument, not owned by our session, so no `atexit` hook,
excepthook or signal handler can be relied on to clean it up.

**The keepalive now runs a finite trigger count that the drain renews**: sized
for ~2 s of acquisition at the rate it has measured, clamped to [50, 500] —
always well under the reading memory — and renewed once half the count has been
used, or sooner if the measured rate shows the count in force is more than twice
what it needs to be. The instrument enforces the deadman itself: a dead app, a
3 s console stall or a 15 s screen capture all end with the acquisition simply
expiring.

Measured against the instrument on VOLT:DC at NPLC 10, panel occupancy sampled
from an independent socket exactly as the table above was:

| Idle pattern | Digits visible | Reading memory | Error queue |
|---|---|---|---|
| `TRIG:COUN INF`, drained every beat | 12/12 | 1 | clean |
| **Finite renewed count (50), keepalive alone** | **11/12** | **0** | **clean** |
| Finite renewed count, with the app's 5 s state poll | 10/12 | 1 | clean |

`TRIG:COUN` cannot be written while an acquisition is in progress — the
instrument answers `+263,"Not able to execute while instrument is measuring"`
and the old count stands — so a renewal costs an `ABOR`/`TRIG:COUN`/`INIT` and
blanks the display for one integration period. **That is a real, measured cost:
one sample in twelve, against 12/12 for `INF`.** At NPLC 10 the count settles at
50 and renews every ~8.6 s, so the blank is ~0.4 s in every ~8.6 s. It is
accepted because an unbounded acquisition that outlives the process is the worse
failure.

The third row is the cost of the other half of the change: every operation that
holds the instrument gate now stands the keepalive's acquisition down first and
restarts it afterwards, so nothing free-runs undrained behind a long call. The
5 s state poll holds the gate for ~0.8 s, and that plus one integration is the
extra blank. With the count now finite this stop is belt-and-braces rather than
the thing preventing an overflow.

Hard kill verified: a process terminated outright mid-acquisition left **28
readings** in memory 8 s later, not climbing, with `+0,"No error"` — the count
had expired on its own.

#### Measured 2026-08-27 — the transport, not the trigger count, is what makes a crash safe

The finite renewed count above protects the *idle keepalive*. It cannot protect
**streaming**, which deliberately runs `TRIG:COUN INF`: any count small enough
to act as a deadman would inject an `ABOR` gap into the measurement every
~1.4 s. Keysight's own software does not have this problem, and the reason was
found by testing the transport rather than the trigger system.

A raw socket on 5025 has no session semantics at all, so the instrument cannot
tell a dead client from a quiet one. Measured with `TRIG:SOUR IMM; TRIG:COUN
INF; SAMP:COUN 1; INIT` running and the client ended by `TerminateProcess`, so
that no handler of ours could possibly run, observed from an independent
session:

| Transport | Resource | After a hard client kill | Panel left |
|---|---|---|---|
| Raw socket | `192.0.2.50:5025` | still acquiring at t+60 s | frozen, in remote |
| HiSLIP | `hislip0`, port 4880 | still acquiring at t+60 s | frozen, in remote |
| **VXI-11** | `inst0`, RPC via port 111 | **aborted and reading memory cleared at t+2 s** | **free-running, 2.9 rdg/s** |

**HiSLIP does not help and is not implemented.** On this firmware the
acquisition survives even a *clean* HiSLIP session close, so it buys nothing
over the raw socket. VXI-11 does help: the instrument's own RPC server performs
a device clear when the last VXI-11 link is destroyed, and destroying the link
is something the operating system does for us when the process dies. That is a
deadman the instrument enforces, which is the only kind that survives a crash.

Two measured caveats:

- The clear fires when the **last** VXI-11 link goes, not when *a* link goes.
  Verified by holding a second, idle VXI-11 link open across the kill: the
  acquisition then kept running. This application holds exactly one link
  (rule 3), so the protection applies — but it is suspended while some *other*
  VXI-11 client (Connection Expert, BenchVue) is also connected to this meter.
- There is no idle-session timeout to lean on instead. Raw socket, HiSLIP and
  VXI-11 sessions all sat completely silent for 180 s and were all still alive
  afterwards. The VXI-11 cleanup is driven by the transport dying, nothing else.

Also measured: at least 5 concurrent VXI-11 links are allowed, a new link can
be created 0.04 s after the previous one died, and VXI-11 costs nothing in
throughput — 853 rdg/s streaming at NPLC 0.02 and a 277 KB screen grab in
194 ms, against 824 rdg/s and 158 ms for the raw socket. Query round-trip is
9.2 ms against 1.3 ms, still 109 ops/s, far above the 40 ops/s ceiling of
rule 5. Only the core channel is opened, so rule 3's "one persistent
connection" still holds exactly.

#### Superseded 2026-08-27 — the count is now conditional on the transport

The paragraph this replaces argued the finite count should stay unconditionally,
because it also bounds memory when the app is alive but not draining. On review
that residual case does not justify its cost:

- the `-365` measurements below defuse it — reading memory saturates at ~10,400
  and recovers from a plain `ABOR`, so an undrained acquisition is untidy, not
  damaging;
- the gate-hold rule already covers the realistic stalls: every multi-exchange
  gate holder stands the idle acquisition down first, so a long passthrough or a
  15 s screen capture does not leave anything free-running behind it;
- against that, the renewal blanks the reading for one integration period every
  ~7.5 s, for ever, on the bench meter of a user who has already been given a
  frozen panel once.

**The keepalive therefore runs `TRIG:COUN INF` when the link is crash-safe, and
the finite renewed count when it is not.** The choice is read from the transport
*actually in force*, not from what was asked for, so an `auto` run that fell back
to the raw socket gets the finite count; and it is re-checked on every renewal
pass, so a link rebuilt on a different transport adopts the right strategy
instead of keeping whichever it started with. `--finite-trigger-count` forces the
finite count on regardless, for belt as well as braces.

**On the raw socket the finite count is unchanged and remains the only
protection there is.** Streaming still uses `TRIG:COUN INF` on both transports —
it always did, and only the transport can protect it.

Measured over 30 s of idle keepalive at NPLC 10, counting the re-arming events
themselves and the gap they leave in the keepalive's own reading stream (no
extra instrument traffic — these are the readings it already drains):

| Link | Deadman | Re-arms in 30 s | Median beat | Worst beats |
|---|---|---|---|---|
| **VXI-11, `INF`** | instrument | **0 — never broken** | 334 ms | 383 / 391 / 397 ms |
| Raw socket, count 50 | trigger count | 4 (~every 7.5 s) | 334 ms | 678 / 704 ms |
| VXI-11, `--finite-trigger-count` | trigger count | 4 (~every 7.5 s) | 339 ms | 684 / 689 ms |

The 678–704 ms outliers are the renewal: roughly double the 343 ms integration,
which is precisely the one-blanked-integration cost the table further up measured
as 11/12 rather than 12/12. Under `INF` there is no such outlier at all, so the
blink is gone at its cause rather than merely made rarer.

Hard kill re-verified on both, with nothing else connected during the
observation (a held monitor socket keeps the meter in remote and masks the
answer):

| Link | Keepalive | After `TerminateProcess` |
|---|---|---|
| VXI-11 | `TRIG:COUN INF` | memory cleared, **panel free-running at 2.9 rdg/s** |
| Raw socket | finite count 50 | 40 readings, **static**, `TRIG:COUN?` still `+50` — the count expired |



#### Measured 2026-08-27 — `-365` is not the hazard it was assumed to be

Recorded because the prohibition above is justified partly by a `-365` overflow
risk, and the risk turns out to be mild. Deliberately overfilled at NPLC 0.02
(~840 rdg/s) with nobody draining:

| Elapsed | `DATA:POIN?` | `STAT:QUES:COND?` | Error queue |
|---|---|---|---|
| 5 s | 4307 | 0 | `+0,"No error"` |
| 9 s | 7746 | 0 | `+0,"No error"` |
| 13 s | 10370 | **16384** | `+0,"No error"` |
| 25 s | 10473 | 16384 | `+0,"No error"` |

Reading memory on this unit holds about **10,400** readings — not the 1000 of
the documentation, nor the 1358 measured earlier. Past that it simply
saturates: the count stops growing, the overflow is reported as bit 14 of the
questionable-data register and **never reaches the error queue at all**. The
instrument kept measuring throughout (`DATA:LAST?` changed on every look),
never wedged, and recovered completely from a plain `ABOR` + drain — 10262
readings retrieved, error queue clean, a normal `READ?` immediately afterwards,
HTTP canary 10 ms.

So a runaway acquisition after a crash is **untidy rather than damaging**. What
it actually costs is that the meter is left frozen in remote, and that the next
session's first drain would deliver up to ten thousand stale readings into the
chart as though they were live. Those are the faults the VXI-11 transport fixes;
they are worth fixing, but they are not the instrument-damaging emergency the
earlier note implied.

### 2. Restore the instrument on disconnect

On clean shutdown, and on window close, the application:

- sends `ABOR` and restores the user's saved trigger configuration
- leaves the trigger system in a state where the front panel free-runs again,
  never ABORted and idle
- attempts to return the instrument to local control (see rule 6)
- closes the socket explicitly (there is only one — see rule 3)

Verified 2026-08-27. Left ABORted and idle the panel took **0 readings** in a
silent 10 s window — frozen, the reported fault. After `ABOR`, restoring the
user's trigger setup and `SYST:LOC`, it free-ran at **2.9 rdg/s** with nobody
connected, and the screen grab shows the Remote annunciator cleared and the
header back to "Auto Trigger". Note that handing back with `SYST:LOC` is what
restarts the panel: there is deliberately no `INIT` left running afterwards, so
there is no acquisition of ours to fill reading memory once the app is gone.

### 3. One persistent socket — no churn, no second link

Because screen polling is withdrawn (rule 4), the `view` socket had no remaining
purpose and was removed.

- **One** `ctrl` socket is opened for the application's lifetime and reused.
  Never one per operation, per poll, or per panel refresh.
- Reconnection happens only after a genuine link failure, and then with backoff:
  1 s, 2 s, 5 s, 10 s, capped at 30 s. Never in a tight loop.
- No second concurrent socket is opened. The instrument supports two, but they
  are no longer needed, and fewer sessions is materially gentler on its LAN
  stack.
- On the VXI-11 transport this means exactly one link, and therefore one TCP
  connection: only the core channel is opened, never the asynchronous abort
  channel. Holding a second VXI-11 link would also suspend the crash cleanup
  described under rule 1.

### 4. No screen polling at all — single-shot capture only

**Continuous screen mirroring is withdrawn entirely.** The app already knows the
function, range, reading, units, trigger mode, NPLC and every math state, and
renders them natively. Polling a 277 KB bitmap to redisplay information the app
already holds is redundant, and it was the single heaviest load on the
instrument's Windows CE LAN stack.

- There is no capture thread and no periodic frame fetching.
- `HCOP:SDUM:DATA?` remains available as an explicit **user-initiated single
  capture** — a "Capture screen" button — for documentation and reporting. One
  grab per click, never a loop, never a timer.
- A single grab runs on the one `ctrl` socket. It blocks for ~0.16 s, which is
  acceptable for a deliberate user action; the idle heartbeat or streaming drain
  is briefly paused around it rather than a second socket being opened.
- The captured frame is shown with its capture timestamp, alongside "Save as
  PNG". It is a snapshot, and is never presented as live.
- When a capture fails, the real error is surfaced and the retry is left to the
  user.

### 5. Global SCPI rate limit

A single ceiling across every link in the process, enforced in the transport
layer so no caller can bypass it: **no more than 40 SCPI operations per second
sustained**.
A `R?` block fetch counts as one operation regardless of how many readings it
returns. Panel/state refreshes are coalesced rather than issued per widget.

### 6. Return to local

There is an explicit **"Return to Local"** action in the UI, and shutdown takes
the same path.

`SYSTem:LOCal` was **not confirmed supported on this model**, and an unsupported
command makes the instrument go silent and poisons the stream (SPEC.md section
2.2), so it was not sent from the application until it had been tested in
isolation on a throwaway socket. The alternative outcome was prepared for: if it
had hung, the front-panel **[Local]** key would have been the only route, and
the UI would have had to say so.

#### Probe result — 2026-08-27: SUPPORTED, and now in use

Tested in isolation: one throwaway socket, 3 s timeout, nothing else on the
link. The error queue was drained to `+0,"No error"`, `SYST:LOC` was sent as
a write, and the queue was read back.

| Check | Result |
|---|---|
| Answer to `SYST:ERR?` after `SYST:LOC` | `+0,"No error"` in 96 ms |
| Socket afterwards | still in sync — `*IDN?` returned the correct identity |
| Hang / silence | none; no reconnect needed |

`SYSTem:LOCal` is therefore **supported on this unit** (34461A, firmware
A.03.03) and the application uses it. The front-panel **[Local]** key is not the
only route, so the UI does not need to say that it is.

**It has to be the last command sent on the link.** Any SCPI command over LAN
puts the instrument straight back into remote, so a `SYST:ERR?` asking whether
`SYST:LOC` worked undoes the very thing it is checking. Measured: with the error
check placed *after* it, the panel took **0 readings** in a silent 12 s window
(still remote and idle); with the check moved *before* it, the panel free-ran at
**2.9 rdg/s**. The error queue is therefore drained before, never after.

## How this was verified — gently, and in this order

The instrument is a physical device on someone's bench, so the testing was
deliberate rather than long and unattended. This is the sequence that was used,
and the one to repeat against this hardware:

1. **Isolated `SYSTem:LOCal` probe.** One throwaway socket, one command, 3 s
   timeout, accepting that it might hang and require a reconnect. The answer is
   recorded under rule 6.
2. **Idle keepalive, ~30 s.** One socket, no screen capture: connect, enter idle
   mode, and check that the front panel keeps updating live rather than
   freezing. This is the test that proves the core fix.
3. **Streaming, ~30 s.** The panel stays live and the rate is sustained.
4. **One on-demand screen capture while streaming.** The grab succeeds, the
   readings resume cleanly afterwards, and `SYST:ERR?` is still empty.
   (The old "mirror at 1 fps" step is void — screen polling is withdrawn.)
5. **Clean shutdown.** The front panel free-runs afterwards, the socket is
   closed, and no threads remain.
6. Between each step, `SYST:ERR?` is checked for empty and the web server at
   `http://192.0.2.50/` for a prompt answer — that is the early warning that
   the LAN stack is being stressed. If it slows, the right move is to stop
   immediately rather than press on.

   **Baseline for "promptly", measured on the healthy instrument before any
   test traffic:** the index page `/` takes **5.1 s** (1.95 s to first byte) for
   its 18 KB, every single time. That is this WinCE server's normal cost for
   that page and is *not* a degradation signal — a small 404 on the same server
   answers in **25 ms**, and TCP accept is 30 ms. Use the small-request latency
   as the sensitive canary and compare `/` against its own 5.1 s baseline;
   judging `/` against an expectation of "fast" raises a false alarm on a
   perfectly healthy meter.

Multi-minute soak tests come only after every step above passes.
