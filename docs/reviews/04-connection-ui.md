# Connection UI / bridge review findings

Independent cold review. Ranked. Several can leave an instrument in remote or
corrupt the saved library. The reviewer sent no SCPI to the meter — findings were
traced in code and reproduced offline against a refusing localhost port and a
QSettings corruption matrix.

## CONFIRMED — can leave an instrument in a bad state

**1. `_thread_finished` releases whatever worker is current, not the one whose thread ended.**
`app/ui/bridge.py:899-924`. Queued slot, no sender/thread check; unconditionally
calls `_release_worker()` and clears `worker`/`thread`/`host`. Reproduced:

    after stale _thread_finished: worker None thread None host ''
    old worker object still alive: True   its thread still running: True
    request_stop now returns: False

Consequences in order: every signal is disconnected from a **running** worker
holding an open link; `request_stop()`/`is_running()` answer False forever so
`closeEvent` (`main.py:1518`) takes the `else` branch and **closes the window
with the link still open — no `ABOR`, no trigger restore, no `SYST:LOC`**; the UI
then tells the user *"the trigger setup was restored and the front panel is
free-running again"*, which is false; and `thread.deleteLater()` is queued
against a running QThread, which Qt treats as fatal.
Deterministic via `Bridge.stop()` (`:875`), which `wait()`s without pumping the
event loop. Fix is one line — capture the thread and compare it to the sender.
This is the worst failure mode in the codebase; do not leave it resting on event
ordering.

**2. The model guard does not stop the keepalive.**
`app/instrument.py:426`, `app/ui/bridge.py:165-176`, `app/ui/main.py:1055-1090`.
`_model_hold` gates `_guard`, `_poll_state` and `_supervise`, but the keepalive is
a plain thread started inside `open()` before `*IDN?` is ever examined. While the
modal dialog waits — potentially minutes — it keeps sending `TRIG:SOUR?`, `ABOR`,
`TRIG:SOUR IMM`, `TRIG:COUN <n>`, `SAMP:COUN 1`, `INIT`, `DATA:POIN?`, `R?` to an
instrument the user has not consented to drive. The docstrings at
`bridge.py:167-171` and `main.py:1060-1064` claiming nothing else is sent are
inaccurate and must be corrected along with the behaviour. Hold the keepalive
until the guard is answered.

**3. Range enumeration before the guard — safe, but it is what makes the guard late.**
`instrument.py:408-428` runs `*CLS`, `*IDN?` and ~24 `<p>:RANG? MIN|MAX` as one
unit. On an unknown model those are unsupported queries whose measured failure
mode is a hung socket. Traced: the handback does happen (timeout → reset →
`open()` raises → `openFailed` → `request_stop` → `close()` sends `ABOR` +
`SYST:LOC`), so nothing is stranded — but the guard never fires and the user only
sees "cannot reach". Note the dependency is implicit: `open()` deliberately does
not close on failure, so the handback exists only because `_on_open_failed`
always tears the worker down. Make that explicit or restructure `open()` so the
model is checked before enumeration.

**4. `returnToLocal` failure leaves a remote, idle instrument with no retry.**
`app/ui/bridge.py:588-628`. `_handed_back = True` and both timers are stopped
*before* `dmm.return_to_local()`. If it raises after `ABOR` and the trigger
restore have gone out, the flag stays set and the timers stay stopped, so the
meter sits in remote, aborted and idle — the frozen panel rule 2 exists to
prevent — until the user happens to press something. Restore state and re-arm on
failure.

**5. Reconnect supervisor bypasses the model guard.**
`app/ui/bridge.py:309-325`. `_supervise` calls `try_open()` then goes straight to
`identityReady`/`_emit_state()`/`_read_system()` with no `is_verified()` check. A
link that drops and returns to a *different* instrument at that address (DHCP
reassignment, a meter swapped on the bench, a forwarder repointed) gets the full
34461A command set with no dialog — and `_on_identity` silently renames the saved
entry to the new instrument's IDN.

## CONFIRMED — data integrity and correctness

**6. `_connected_index` is never remapped after the dialog edits the library.**
`app/ui/main.py:766-783`, `app/ui/connect.py:663-673`. The dialog tracks its own
index correctly but never returns it. Remove an entry *above* the connected one
and the index is off by one for the session: the "connected" mark and menu check
point at the wrong meter, the window title names the wrong meter, and
`_on_identity` writes this instrument's `MODEL SERIAL` label onto **a different
saved entry** — persistent corruption of the library. Also makes
`_picker_changed` compare a stale index, so re-selecting the current meter forces
a full teardown and rebuild.

**7. dB/dBm: nothing clears the chart or log when scaling toggles.**
`app/ui/main.py:1107-1117` clears only on a *function* change, while
`chart.py:821-824` relabels the axis immediately. Toggling `CALC:SCAL:STAT`
mid-session leaves volt samples in the ring buffer under a dB axis, autoscaled
together, with the crosshair reporting both in the new unit. Worse,
`logtab.py:119` formats **every historical row** with the current state, so a
table of volts silently becomes a table of dB. Every *derivation* site was
covered correctly; the gap is buffer invalidation. Clear on scaling transitions
as well as function changes.

**8. CSV unit is frozen at `start_log`.** `app/instrument.py:2359-2382`, used at
`:2411`, `:2425`, `:2445`. `_log_unit` is set once, so toggling scaling during a
recording yields a file whose header and every row claim the unit in force when
Record was pressed. Mirror image of finding 7.

**9. The resolution-band caption is clipped at the default window size.**
`app/ui/readout.py:686-698`. Single `drawText` with no `TextWordWrap` and no
elide into a 24 px strip; the dB caption is ~150 characters. Visible in
`screenshots/db-2-window-db.png`: *"…Every digit is solid and no"* — cut
mid-sentence. **The clipped half is precisely the sentence saying no accuracy
figure is being shown**, which is the honesty this element exists to provide.

**10. A single CLI run permanently rewrites the saved entry.**
`app/ui/main.py:1650-1657`. `--transport` and `--finite-trigger-count` are
written into the matched entry and saved. One `--finite-trigger-count` run leaves
that meter permanently on the finite count — a blanked reading every few seconds
on its front panel, forever, with nothing on screen explaining why. CLI flags
should apply to the session, not mutate saved settings, or must be explicit about
doing so. Also `DMM_FINITE_TRIGGER_COUNT=false` currently reads as true
(`:1611`).

**11. Handover watchdog timers are never cancelled and are not tied to their handover.**
`app/ui/main.py:883`, `:930`, `:1516`, handler at `:985`. Each handover arms a
fresh 30 s `singleShot`; earlier ones stay live, guarded only by
`self._phase != "disconnecting"`. A stale timer can declare failure on a healthy
handover: clears `_pending_index` so a *switch* becomes a plain disconnect and
the user never reaches meter B, flips the phase while the real worker is still
shutting down, and warns that a correctly handed-back meter may still be in
remote. Switching around a bench is exactly the workload that arms these
repeatedly.

**12. After a genuine handover timeout the next Connect is guaranteed to fail.**
`app/ui/main.py:876-885`. The phase goes "offline" and controls re-enable while
the old worker is still stopping; the next Connect takes the `is_running()`
branch, `request_stop()` returns False, control falls through to `_start_pending`
which consumes `_pending_index`, and `Bridge.connect_to` then correctly refuses.
Rule 3 holds — no second link — but the request is silently dropped and the phase
machine must be re-driven by hand.

**13. `shutdown()`/`_release_worker()` have no last-resort guard.**
`app/ui/bridge.py:213-234` catches only `ScpiError`; `_release_worker` (`:906`)
uses bare `disconnect()`. Anything escaping either skips `stopped.emit()` /
`finished.emit()`, so the thread never quits and neither a switch nor a window
close can complete — the user waits out the 30 s watchdog and the process exits
with the link open. Probed clean on the normal path, so this is fragility rather
than a live bug, but it is the single point where handing the instrument back
depends on nothing unexpected being raised.

**14. Two settings-corruption cases wipe the library silently.**
`app/ui/connect.py:219-237`. A value stored as a string list (`type=str` yields
`""`, so the `if raw:` branch is skipped) and a JSON list whose items are not
dicts (filtered by `isinstance`) both lose every saved instrument with **no**
`load_error`. Contradicts ARCHITECTURE.md §1's no-silent-failure rule for the one
file the user cannot rebuild from the instrument. Everything else in this area is
clean — unicode round-trips, bad `selected` falls back to 0, an empty address
survives load and is caught on Connect, and a corrupt library never blocks
startup.

**15. Switching meters leaves the previous instrument's data on screen.**
`app/ui/main.py:934-947`. `_reset_for_new_link` clears the chart, log, readout,
live dot, capture and ident fields — but not the System panel (IDN, MAC, LAN,
**calibration date**, self-test), Statistics, Histogram or the Limits verdict.
After a switch these show meter A's figures under meter B's title; if B then
fails to connect, meter A's serial and cal date stay on screen while the app is
offline. Unacceptable in a metrology tool.

**16. `_prune_empty` shifts the selection when it drops a blank entry above it.**
`app/ui/connect.py:681-692`. Clamps but does not remap: `[blank, A, B]` with A
selected becomes `[A, B]` with **B** selected. Nearly unreachable today, but the
same index-vs-identity class as finding 6; `_connect` already shows the right
pattern (`index_of(entry)` after pruning).

**17. Design deviation: Martian Mono in the connect dialog.**
`app/ui/connect.py:366-370` uses `theme.readout(15)` for the `34461A` mark.
ARCHITECTURE.md §5 restricts that face to the readout and headline numerics; a modal
dialog is the one place ruled out. Everything else conforms — menus/buttons/body
in IBM Plex Sans, captions in IBM Plex Mono, palette tokens throughout, focus
rings visible, no unstyled default widgets, usable at the 720x440 minimum.

**18. Minor lifecycle**
- `bridge.py:906-924`: no `worker.deleteLater()`. The dropped worker and its
  `Dmm34461A` — up to ~150 MB of log buffer — survive until the cycle collector,
  then are destroyed from the GUI thread with `QTimer` children whose affinity
  was the dead worker thread.
- `bridge.py:259-283`: `_guard`'s implicit `try_open()` is a second reconnect
  path that ignores `_ever_connected`, so after a failed first connect the 5 s
  poll re-attempts the bad address despite `_supervise` being held off for
  exactly that reason.
- `main.py:724-733`: the Instruments menu calls `connect_to_index(i)`
  unconditionally, so choosing the meter you are already on performs a full
  rebuild. The picker guards this; the menu does not. Rule 3 forbids link churn.
- `main.py:672-684`: `Ctrl+R`/`F5` are not disabled by `_set_phase`, so a
  shortcut during a handover is dropped silently with no feedback.

## SUSPECTED

**A. GUI reachability of finding 1.** Requires `connect_to()` between the old
thread returning from `run()` and delivery of its posted `finished`. Qt drains
posted events ahead of native input, so no GUI sequence was constructed. Treat as
a latent invariant violation with a deterministic non-GUI trigger.

**B. Digit geometry moves under dB.** `app/ui/readout.py:145-160`. With `scaled`
set, the range is rejected as the sizing reference and `magnitude_hint` is used,
so a reading crossing a decade (−9.9 → −10.1 dB) re-lays out the field, which
ARCHITECTURE.md §3 says must never happen. There is genuinely no configured span to
size a logarithmic reading from, so this reads as a knowing trade — but decide it
deliberately rather than by omission.

## Confirmed clean — do not churn

Rule 3 (exactly one link) on every path, including the retry after a partial open
and both reconnect routes; widget connections all bind to `Bridge`, which
outlives every worker, and `_release_worker` unhooks both directions; shutdown
ordering (`worker.stopped → thread.quit` as a `DirectConnection`, `publish`
rebound to `_discard` before deletion); switching while streaming (`close()`
stops both acquisition modes, confirms the threads are gone, then restores the
trigger and sends `SYST:LOC` last — and deliberately withholds `SYST:LOC` when a
thread will not stop, telling the user to press [Local], which is the right call);
SPEC §2.2 enforcement in the transport below every caller; the overload sentinel
end-to-end with no synthesised accuracy figure anywhere; and the
transport/`crash_safe`/keepalive indicators, which read from the live state and
are nulled on a switch.
