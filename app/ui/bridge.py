# Bench Buddy - desktop control console for the Keysight 34461A multimeter.
# Copyright (C) 2026 zombu4
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.

"""The Qt <-> instrument threading bridge.

``Dmm34461A`` is blocking and thread-confined.  A 0.16 s screen grab or a 3 s
SCPI timeout must never run on the GUI thread, so:

* one :class:`QThread` owns the instrument object and every call into it;
* the GUI reaches it only by emitting a :class:`Bridge` signal, which Qt
  delivers to the worker as a queued event — there is no direct call path
  from a widget into ``instrument.py``;
* results come back as worker signals, delivered to the GUI thread as queued
  events in turn;
* the streaming and idle-keepalive threads inside ``instrument.py`` publish
  through :class:`_Publisher`, a ``QObject`` owned by the worker, so their
  callbacks are marshalled onto the worker thread instead of running on
  whichever instrument thread happened to produce the message.

Nothing in this module touches a widget.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, Signal, Slot

from ..instrument import Dmm34461A
from ..models import is_verified
from ..scpi import ScpiError

# How often the worker re-reads the full State object.  One read costs ~30
# round trips, so at the old 1 s period it alone accounted for most of the 40
# operations per second IO-DISCIPLINE.md rule 5 allows.  Every user action
# already emits a fresh state of its own; this periodic read exists only to
# notice changes made at the front panel, which does not need to be quick.
# Slowing it is the "coalesce state refreshes" half of rule 5.
STATE_PERIOD_IDLE_MS = 5000
STATE_PERIOD_RUNNING_MS = 10000
# Rule 3: how often to check whether a dropped link may be re-opened.  The
# link's own 1/2/5/10/30 s backoff decides whether an attempt actually goes
# out, so this timer cannot turn into a reconnect loop.
RECONNECT_PERIOD_MS = 2000


class _Publisher(QObject):
    """Turns ``Dmm34461A.publish`` messages into Qt signals.

    Created on the worker thread, so it has the worker's thread affinity.
    ``publish`` is called from the streaming and idle-keepalive threads, which
    are plain ``threading.Thread``s with no event loop of their own; the
    connection to :meth:`InstrumentWorker._on_publish` is explicitly queued, so
    the fan-out runs on the worker thread and the second hop from there to the
    GUI is queued in turn.

    It was a ``DirectConnection``, which meant ``_on_publish`` — and every
    ``emit`` it makes — ran on the instrument's own drain thread.  That worked
    only because the second hop happened to be cross-thread and so queued
    itself; a receiver that ever landed on the drain thread would have been
    called from inside the drain, holding the instrument gate.
    """

    message = Signal(object)

    def dispatch(self, message: Dict[str, Any]) -> None:
        self.message.emit(message)


class InstrumentWorker(QObject):
    """Every instrument call in the application is a slot on this object."""

    stateReady = Signal(object)  # the full State dict of SPEC.md 4.1
    dataReady = Signal(object)  # {"type":"data", t, v, ovld?}
    readingReady = Signal(object)  # {"type":"reading", t, v, ovld} idle beat
    statsReady = Signal(object)
    limitReady = Signal(object)
    errorRaised = Signal(str)  # a real failure, with the instrument's own text
    noticeRaised = Signal(str)  # a completed action worth confirming
    systemReady = Signal(object)
    histogramReady = Signal(object)
    scpiReady = Signal(object)
    singleReady = Signal(object)
    selftestReady = Signal(object)
    frameReady = Signal(object, float)  # PNG bytes, epoch of the capture
    localReady = Signal(object)  # result of returning the instrument to local
    exportReady = Signal(str, int)  # path, bytes written
    linkChanged = Signal(bool, str)  # connected, detail
    identityReady = Signal(object)  # the parsed *IDN? of the link just opened
    openFailed = Signal(str)  # the first connection attempt failed outright
    modelUnverified = Signal(object)  # *IDN? named a model SPEC.md never probed
    stopped = Signal()

    def __init__(
        self,
        host: str,
        port: int = 5025,
        transport: str = "auto",
        force_finite_count: bool = False,
    ) -> None:
        super().__init__()
        self.host = host
        self.dmm = Dmm34461A(host, port, transport, force_finite_count)
        self._publisher: Optional[_Publisher] = None
        self._state_timer: Optional[QTimer] = None
        self._reconnect_timer: Optional[QTimer] = None
        self._last_connected: Optional[bool] = None
        self._closing = False
        # Set once the instrument has been handed back to its front panel.
        # While it is set nothing in this worker may touch the link: any SCPI
        # command puts the meter straight back into remote, which is exactly
        # what "Return to Local" was pressed to undo.
        self._handed_back = False
        # True once a link has actually been established at least once.  Until
        # then a failure is a bad address rather than a dropped link, so it is
        # reported through openFailed and the reconnect supervisor stays off:
        # the application must not sit retrying an address the user mistyped.
        self._ever_connected = False
        # Set when *IDN? named a model this repository's command set was never
        # verified against (app/models.py).  While it is set **nothing at all
        # goes out on the link**: the Qt-side callers are gated by _guard,
        # _poll_state and _supervise; the range enumeration is held inside
        # Dmm34461A.open by the gate _open_link passes it; and the idle
        # keepalive — a plain thread, which no Qt gate reaches — is simply not
        # started until the user answers.  The decision is therefore taken
        # after *CLS and *IDN? and before anything else, which is what makes
        # it worth asking.
        self._model_hold = False
        # The ``*IDN?`` of the first link this worker opened.  A reconnect
        # that answers with a different serial is a different instrument at
        # the same address, and the user is told rather than left to notice
        # that the saved entry has quietly renamed itself.
        self._first_identity: Optional[Dict[str, str]] = None
        # Whether shutdown() has already said how the handback went.  If it
        # never gets that far, the failure is reported in its place rather
        # than leaving the window to claim the panel is free-running again.
        self._handback_reported = False

    # ------------------------------------------------------------ lifecycle

    @Slot()
    def start(self) -> None:
        """Runs on the worker thread, once its event loop is up."""
        self._publisher = _Publisher()
        self._publisher.message.connect(self._on_publish, Qt.QueuedConnection)
        self.dmm.publish = self._publisher.dispatch

        self._state_timer = self._timer(self._poll_state, STATE_PERIOD_IDLE_MS)
        self._reconnect_timer = self._timer(self._supervise, RECONNECT_PERIOD_MS)
        self._open()

    def _timer(self, slot: Callable[[], None], period: int) -> QTimer:
        timer = QTimer(self)
        timer.setTimerType(Qt.CoarseTimer)
        timer.timeout.connect(slot)
        timer.start(period)
        return timer

    def _on_publish(self, message: Dict[str, Any]) -> None:
        """Fan one published message out to the matching GUI signal."""
        kind = message.get("type")
        if kind == "data":
            self.dataReady.emit(message)
        elif kind == "reading":
            # An idle heartbeat reading: it keeps the readout live but is not
            # part of a run, so it never reaches the chart or the log.
            self.readingReady.emit(message)
        elif kind == "state":
            self.stateReady.emit(message.get("state") or {})
        elif kind == "stats":
            self.statsReady.emit(message)
        elif kind == "limit":
            self.limitReady.emit(message)
        elif kind == "error":
            self.errorRaised.emit(str(message.get("message", "")))
        else:
            self.errorRaised.emit(
                f"unrecognised message from the instrument layer: {kind!r}"
            )

    def _open_link(self) -> str:
        """Open the link and decide whether it may be driven.

        Returns ``"ready"``, ``"held"`` (the model guard is waiting for the
        user) or ``"failed"``.

        **Nothing beyond ``*CLS`` and ``*IDN?`` is sent to a model this
        command set was never verified against.**  Two things make that true
        rather than approximately true:

        * the range enumeration — ~24 ``<p>:RANG? MIN|MAX`` queries whose
          measured failure mode on an unknown model is a hung socket — is now
          held behind the same decision, by passing the check into
          :meth:`Dmm34461A.open` as the gate between ``*IDN?`` and the
          enumeration (finding 3);
        * the idle keepalive is not started here.  It is a plain thread, not
          a Qt timer, so ``_model_hold`` never gated it: while the modal
          dialog waited — potentially minutes — it kept sending ``TRIG:SOUR?``,
          ``ABOR``, ``TRIG:SOUR IMM``, ``TRIG:COUN <n>``, ``SAMP:COUN 1``,
          ``INIT``, ``DATA:POIN?`` and ``R?`` to an instrument the user had
          not consented to drive (finding 2).

        :meth:`proceedUnverified` completes both steps when the user accepts.
        """
        verified = True

        def gate(identity: Dict[str, str]) -> bool:
            nonlocal verified
            verified = is_verified(identity.get("model", ""))
            return verified

        if not self.dmm.try_open(on_identity=gate, start_heartbeat=False):
            return "failed"
        self._ever_connected = True
        identity = dict(self.dmm.identity)
        self._announce_link(True, "")
        previous = self._first_identity
        if previous is None:
            self._first_identity = dict(identity)
        elif (identity.get("serial") or "") != (previous.get("serial") or ""):
            self.errorRaised.emit(
                f"the instrument at {self.host} is not the one this link "
                f"started with: it now answers "
                f"{identity.get('model', '')} {identity.get('serial', '')}, "
                f"not {previous.get('model', '')} {previous.get('serial', '')}. "
                f"Check which meter this address reaches before trusting the "
                f"readings, and note that the saved entry follows the "
                f"instrument that answered."
            )
        self.identityReady.emit(identity)
        if not verified:
            self._model_hold = True
            self.modelUnverified.emit(identity)
            return "held"
        self.dmm.start_heartbeat()
        return "ready"

    def _open(self) -> None:
        outcome = self._open_link()
        if outcome == "ready":
            self._emit_state()
            self._read_system()
        elif outcome == "failed":
            detail = self.dmm.last_error or "not connected"
            self._announce_link(False, detail)
            if not self._ever_connected:
                # Never up: this is a wrong or unreachable address, not a
                # dropped link.  Say so once and let the UI tear the worker
                # down, instead of retrying behind a dialog the user is
                # already correcting.
                self.openFailed.emit(detail)
                return
            self._emit_state()

    @Slot()
    def proceedUnverified(self) -> None:
        """Carry on against a model this command set was never verified on.

        The user has been shown which model answered and has chosen to go
        ahead (app/models.py).  Nothing is adapted — the same 34461A command
        set is used — so this lifts the hold and then finishes the open that
        stopped at ``*IDN?``: the range enumeration runs and the idle
        keepalive starts, in that order, because the keepalive needs the
        function metadata and rule 1 wants it running from the moment the app
        is driving the instrument.

        If the enumeration fails — the measured failure mode of those queries
        on an unknown model is a hung socket — this reports it *and* raises
        ``openFailed``, which is what tears the worker down and hands the
        instrument back with ``ABOR`` and ``SYST:LOC``.  Leaving the link open
        after that would leave an unknown meter in remote with nothing
        measuring, which is rule 1's frozen panel.
        """
        if self._closing or not self._model_hold:
            return
        self._model_hold = False
        try:
            self.dmm.finish_open()
        except (ScpiError, ValueError, OSError) as exc:
            detail = f"enumerating this instrument's ranges: {exc}"
            self.errorRaised.emit(detail)
            self.openFailed.emit(detail)
            return
        self._guard("reading the instrument state", self._emit_state)
        self._guard("reading the system information", self._read_system)

    def _announce_link(self, up: bool, detail: str) -> None:
        if self._last_connected != up:
            self._last_connected = up
            self.linkChanged.emit(up, detail)

    @Slot()
    def shutdown(self) -> None:
        """Stop everything the application started, then close the link.

        ``stopped`` **must** be emitted whatever happens in here: it is what
        ends this thread's event loop, and without it neither a switch to
        another meter nor a window close can ever complete — the user waits
        out the 30 s watchdog and the process exits with the link open.  So
        the body is wrapped: a failure is reported with its real text (no
        silent ``except``), and the teardown still runs.
        """
        self._closing = True
        self._handback_reported = False
        try:
            self._shutdown_body()
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            detail = f"{type(exc).__name__}: {exc}"
            self.errorRaised.emit(f"during shutdown: {detail}")
            if self._ever_connected and not self._handback_reported:
                # The handback did not finish, so the window must not be
                # allowed to say the front panel is free-running again.  This
                # is the same false success finding 1 produced, reached by a
                # different route.
                self.localReady.emit({"local": False, "detail": detail})
        finally:
            self.dmm.publish = _discard
            for name in ("_state_timer", "_reconnect_timer"):
                timer = getattr(self, name)
                if timer is not None:
                    try:
                        timer.stop()
                        # Deleted here, on the worker's own thread and while
                        # its event loop is still running, so the object that
                        # outlives this thread has no children with the dead
                        # thread's affinity left to destroy.
                        timer.deleteLater()
                    except RuntimeError:
                        pass
                    setattr(self, name, None)
            if self._publisher is not None:
                try:
                    self._publisher.deleteLater()
                except RuntimeError:
                    pass
                self._publisher = None
            self.stopped.emit()

    def _shutdown_body(self) -> None:
        for timer in (self._state_timer, self._reconnect_timer):
            if timer is not None:
                timer.stop()
        try:
            result = self.dmm.close()
        except ScpiError as exc:
            self.errorRaised.emit(f"during shutdown: {exc}")
        else:
            for problem in result.get("problems") or []:
                self.errorRaised.emit(f"during shutdown: {problem}")
            if self._ever_connected:
                self._handback_reported = True
                self.localReady.emit(result.get("local") or {"local": False})
            # If the link never came up there is nothing to hand back and
            # "the instrument was not returned to local" would be a false
            # alarm about a meter this process never took.

    # ------------------------------------------------------------- plumbing

    def _resume_driving(self) -> None:
        """Take the instrument back after a "Return to Local" handoff.

        The user asked for something that needs the link, so the app is driving
        again: the periodic poll restarts and the idle keepalive goes back on,
        because holding the link with the trigger system idle is what leaves a
        bench meter looking dead (rule 1).
        """
        if not self._handed_back:
            return
        self._handed_back = False
        for timer, period in (
            (self._state_timer, STATE_PERIOD_IDLE_MS),
            (self._reconnect_timer, RECONNECT_PERIOD_MS),
        ):
            if timer is not None:
                timer.start(period)
        if self.dmm.opened and not self.dmm.heartbeat_running:
            self.dmm.start_heartbeat()

    def _guard(self, what: str, action: Callable[[], None]) -> bool:
        """Run one instrument call, reporting any failure with its real text."""
        if self._closing:
            return False
        if self._model_hold:
            self.errorRaised.emit(
                f"{what}: nothing has been sent yet — this instrument did not "
                f"identify itself as a model this application has been "
                f"verified against, and the warning is still waiting for an "
                f"answer."
            )
            return False
        self._resume_driving()
        if not self.dmm.opened:
            # Re-opening here is a *reconnect*, and it obeys the same two
            # rules _supervise does.  A link that was never up is a bad
            # address, and retrying it from the 5 s state poll is the tight
            # reconnect loop rule 3 forbids, aimed at an address the user is
            # in the middle of correcting — _open has already reported that
            # case.  And whatever answers has to be identified before it is
            # driven, because the address may now belong to another meter.
            outcome = "failed" if not self._ever_connected else self._open_link()
            if outcome != "ready":
                if outcome == "held":
                    detail = (
                        "the instrument now answering at this address is not a "
                        "model this application has been verified against; "
                        "nothing further will be sent until the warning is "
                        "answered"
                    )
                else:
                    detail = (
                        self.dmm.last_error or "the instrument is not connected"
                    )
                    self._announce_link(False, detail)
                self.errorRaised.emit(f"{what}: {detail}")
                return False
        try:
            action()
        except (ScpiError, ValueError, OSError) as exc:
            self.errorRaised.emit(f"{what}: {exc}")
            if not self.dmm.connected:
                self._announce_link(False, str(exc))
            try:
                self._emit_state()
            except ScpiError as state_exc:
                self.errorRaised.emit(
                    f"reading the state after that failure: {state_exc}"
                )
            return False
        self._announce_link(True, "")
        return True

    def _emit_state(self) -> None:
        state = self.dmm.read_state()
        self.stateReady.emit(state)
        period = (
            STATE_PERIOD_RUNNING_MS if state.get("streaming") else STATE_PERIOD_IDLE_MS
        )
        if self._state_timer is not None and self._state_timer.interval() != period:
            self._state_timer.start(period)

    @Slot()
    def _poll_state(self) -> None:
        if self._closing or self._handed_back or self._model_hold:
            return
        self._guard("reading the instrument state", self._emit_state)

    @Slot()
    def _supervise(self) -> None:
        """Re-open a link that dropped, without blocking any user action.

        Only a link that was once up is re-opened.  A first attempt that never
        succeeded is a bad address, and :meth:`_open` has already handed that
        to the UI through ``openFailed``; retrying it here would be the tight
        reconnect loop rule 3 forbids, aimed at an address the user is in the
        middle of correcting.

        The re-open goes through :meth:`_open_link`, so the model guard
        applies here exactly as it does to the first connection.  A link that
        drops and comes back to a *different* instrument at that address — a
        DHCP reassignment, a meter swapped on the bench, a forwarder
        repointed — used to be handed the full 34461A command set with no
        dialog, and the saved entry was silently renamed to whatever answered.
        """
        if self._closing or self._handed_back or self._model_hold:
            return
        if not self._ever_connected or self.dmm.opened:
            return
        if self._open_link() == "ready":
            self._emit_state()
            self._read_system()

    # ------------------------------------------------------------ user slots

    @Slot()
    def refreshState(self) -> None:
        self._guard("reading the instrument state", self._emit_state)

    @Slot(str)
    def setFunction(self, key: str) -> None:
        def run() -> None:
            self.dmm.set_function(key)
            self._emit_state()
            self.noticeRaised.emit(f"Function set with CONF:{key}")

        self._guard(f"selecting {key}", run)

    @Slot(object)
    def setConfig(self, changes: Dict[str, Any]) -> None:
        def run() -> None:
            self.dmm.set_config(changes)
            self._emit_state()

        self._guard("applying " + ", ".join(changes), run)

    @Slot(object)
    def setTrigger(self, changes: Dict[str, Any]) -> None:
        def run() -> None:
            self.dmm.set_trigger(changes)
            self._emit_state()

        self._guard("setting trigger " + ", ".join(changes), run)

    @Slot(object)
    def setMath(self, changes: Dict[str, Any]) -> None:
        def run() -> None:
            self.dmm.set_math(changes)
            self._emit_state()

        self._guard("setting math " + ", ".join(changes), run)

    @Slot(object)
    def setDisplay(self, changes: Dict[str, Any]) -> None:
        def run() -> None:
            self.dmm.set_display(changes)
            self._emit_state()

        self._guard("changing the instrument display", run)

    @Slot(bool)
    def setStream(self, run_it: bool) -> None:
        def run() -> None:
            if run_it:
                self.dmm.start_stream()
            else:
                self.dmm.stop_stream()
            self._emit_state()

        self._guard("starting the run" if run_it else "stopping the run", run)

    @Slot()
    def single(self) -> None:
        def run() -> None:
            self.singleReady.emit(self.dmm.single())
            self._emit_state()

        self._guard("taking a single reading", run)

    @Slot()
    def clearStats(self) -> None:
        def run() -> None:
            self.dmm.clear_stats()
            self.statsReady.emit(
                {
                    "avg": None,
                    "min": None,
                    "max": None,
                    "ptp": None,
                    "sdev": None,
                    "count": 0,
                }
            )
            self.noticeRaised.emit("Statistics cleared with CALC:AVER:CLE")

        self._guard("clearing the statistics", run)

    @Slot()
    def clearHistogram(self) -> None:
        def run() -> None:
            self.dmm.clear_histogram()
            self.histogramReady.emit(self.dmm.histogram())
            self.noticeRaised.emit("Histogram cleared with CALC:TRAN:HIST:CLE")

        self._guard("clearing the histogram", run)

    @Slot()
    def readHistogram(self) -> None:
        self._guard(
            "reading the histogram",
            lambda: self.histogramReady.emit(self.dmm.histogram()),
        )

    @Slot()
    def readStats(self) -> None:
        self._guard(
            "reading the statistics", lambda: self.statsReady.emit(self.dmm.stats())
        )

    @Slot(bool, str)
    def setLog(self, run_it: bool, note: str) -> None:
        def run() -> None:
            if run_it:
                self.dmm.start_log(note)
            else:
                self.dmm.stop_log()
            self._emit_state()

        self._guard("starting the log" if run_it else "stopping the log", run)

    @Slot()
    def clearLog(self) -> None:
        self.dmm.clear_log()
        self._guard("reading the instrument state", self._emit_state)
        self.noticeRaised.emit("Log buffer cleared")

    @Slot(str)
    def exportLog(self, path: str) -> None:
        """Stream the log buffer to *path* as CSV, off the GUI thread.

        ``log_csv_chunks`` writes an overloaded reading as an empty value with
        ``overload=1``.  ARCHITECTURE.md section 4 asks for an explicit ``OVLD``
        cell instead, so the value column is filled in on the way out; the
        overload column is left alone, so the file is still machine-readable.
        """
        try:
            written = 0
            with open(path, "w", encoding="utf-8", newline="") as handle:
                for chunk in self.dmm.log_csv_chunks():
                    text = _mark_overloads(chunk)
                    handle.write(text)
                    written += len(text)
        except OSError as exc:
            self.errorRaised.emit(f"writing {path}: {exc}")
            return
        self.exportReady.emit(path, written)

    @Slot(object)
    def saveFrame(self, request: Dict[str, Any]) -> None:
        """Write the PNG bytes the GUI already holds to disk, off its thread."""
        path = str(request.get("path", ""))
        data = request.get("data")
        if not isinstance(data, (bytes, bytearray)) or not path:
            self.errorRaised.emit("saving the screen: no frame has been captured yet")
            return
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            self.errorRaised.emit(f"writing {path}: {exc}")
            return
        self.exportReady.emit(path, len(data))

    @Slot()
    def beep(self) -> None:
        def run() -> None:
            self.dmm.beep()
            self.noticeRaised.emit("SYST:BEEP sent")

        self._guard("beeping", run)

    @Slot()
    def selftest(self) -> None:
        def run() -> None:
            self.selftestReady.emit(self.dmm.selftest())
            self._emit_state()

        self._guard("running the self test", run)

    @Slot(bool)
    def lock(self, acquire: bool) -> None:
        def run() -> None:
            result = self.dmm.lock(acquire)
            if acquire:
                self.noticeRaised.emit(
                    "Front panel locked"
                    if result.get("acquired")
                    else f"Lock refused; the owner is {result.get('owner', 'unknown')}"
                )
            else:
                self.noticeRaised.emit("Front panel lock released")
            self._read_system()

        self._guard("changing the front panel lock", run)

    @Slot()
    def reset(self) -> None:
        def run() -> None:
            self.dmm.reset()
            self._emit_state()
            self.noticeRaised.emit("*RST sent; the instrument is at its power-on setup")

        self._guard("resetting the instrument", run)

    @Slot(str)
    def passthrough(self, command: str) -> None:
        """Send a user-typed command; every outcome reaches the console."""
        if self._closing:
            return
        if not self.dmm.opened and not self.dmm.try_open():
            detail = self.dmm.last_error or "the instrument is not connected"
            self.scpiReady.emit(
                {
                    "cmd": command,
                    "response": "",
                    "is_query": False,
                    "error": detail,
                    "elapsed_ms": 0.0,
                }
            )
            return
        try:
            result = self.dmm.passthrough(command)
        except (ScpiError, ValueError) as exc:
            self.scpiReady.emit(
                {
                    "cmd": command,
                    "response": "",
                    "is_query": False,
                    "error": str(exc),
                    "elapsed_ms": 0.0,
                }
            )
            self._guard("reading the instrument state", self._emit_state)
            return
        result["cmd"] = command
        self.scpiReady.emit(result)
        self._guard("reading the instrument state", self._emit_state)

    @Slot()
    def readSystem(self) -> None:
        self._guard("reading the system information", self._read_system)

    def _read_system(self) -> None:
        self.systemReady.emit(self.dmm.system_info())

    @Slot()
    def captureScreen(self) -> None:
        """One screen grab, because the user pressed the button (rule 4).

        There is no capture thread and no timer behind this: continuous
        mirroring was withdrawn.  Each press costs exactly one 277 KB transfer.
        """

        def run() -> None:
            frame, stamp = self.dmm.capture_screen()
            self.frameReady.emit(frame, stamp)
            self._emit_state()
            self.noticeRaised.emit("Screen captured with HCOP:SDUM:DATA?")

        self._guard("capturing the instrument screen", run)

    @Slot()
    def returnToLocal(self) -> None:
        """Hand the instrument back to its front panel (rule 6).

        ``SYST:LOC`` has to be the genuinely last command on the link, so every
        source of traffic is stopped *before* it goes out and nothing is sent
        after it.  This slot used to emit a fresh state read afterwards — 33
        queries — on top of the 33 ``return_to_local`` itself published, with
        the 5 s state timer still running behind both; the meter was back in
        remote within milliseconds, and because the keepalive is deliberately
        left stopped it was remote *and* idle, which is the frozen panel this
        button exists to cure. The UI was told it had succeeded.

        The state the UI is given afterwards is assembled from what this
        process already knows (:meth:`Dmm34461A.local_state`) and costs no I/O
        at all.
        """
        if self._closing:
            return
        self._resume_driving()
        if not self.dmm.opened and not self.dmm.try_open():
            detail = self.dmm.last_error or "the instrument is not connected"
            self.errorRaised.emit(f"returning the instrument to local: {detail}")
            self.localReady.emit({"local": False, "detail": detail})
            return
        # Stop the periodic poll and the reconnect supervisor first: either one
        # firing after SYST:LOC would undo it.
        for timer in (self._state_timer, self._reconnect_timer):
            if timer is not None:
                timer.stop()
        self._handed_back = True
        try:
            result = self.dmm.return_to_local()
        except (ScpiError, OSError) as exc:
            # The handback did not complete, and ``ABOR`` and the trigger
            # restore have already gone out: the meter is aborted, idle and
            # still in remote — rule 1's frozen panel — and the flag and the
            # stopped timers would have left it that way until the user
            # happened to press something.  Take it back instead: the poll and
            # the reconnect supervisor are re-armed and the keepalive is
            # restarted, so the panel is measuring again while the user
            # decides what to do.
            self._recover_handback()
            self.errorRaised.emit(
                f"returning the instrument to local: {exc}. The application "
                f"has taken the instrument back so its panel is not left "
                f"frozen; press [Local] on the front panel, or try Return to "
                f"Local again."
            )
            self.localReady.emit({"local": False, "detail": str(exc)})
            return
        if not result.get("local"):
            # ``SYST:LOC`` was refused, or something of ours would not let go
            # of the link.  Either way the instrument is still ours, so it is
            # driven rather than left aborted and idle.
            self._recover_handback()
            self.localReady.emit(result)
            # Deliberately no state emission here: ``local_state`` would claim
            # ``local: True`` for a handback that did not happen, and a real
            # read is 33 queries the re-armed 5 s poll is about to make
            # anyway.
            return
        # Nothing below this line may touch the link.
        self.localReady.emit(result)
        self.stateReady.emit(self.dmm.local_state())

    def _recover_handback(self) -> None:
        """Undo a "Return to Local" that did not actually happen.

        ``_resume_driving`` is the same path a later user action would take;
        calling it here means the recovery is the one the rest of the worker
        already trusts — timers re-armed, keepalive restarted if the link is
        still open — rather than a second, parallel idea of what "driving"
        means.
        """
        self._resume_driving()


def _mark_overloads(chunk: str) -> str:
    """Replace the empty value cell of an overloaded row with ``OVLD``.

    Rows are ``index,epoch_s,elapsed_s,value,unit,overload``; the header rows
    start with ``#`` or ``index`` and are passed through untouched.  None of
    the data fields can contain a comma, so a plain split is exact here.
    """
    if not chunk:
        return chunk
    newline = chr(10)
    out = []
    for line in chunk.split(newline):
        if line.startswith("#") or line.startswith("index") or not line:
            out.append(line)
            continue
        fields = line.split(",")
        if len(fields) == 6 and fields[5] == "1" and fields[3] == "":
            fields[3] = "OVLD"
            out.append(",".join(fields))
        else:
            out.append(line)
    return newline.join(out)


def _discard(message: Dict[str, Any]) -> None:
    """The post-shutdown publish sink: the Qt objects are gone by then."""
    return None


class Bridge(QObject):
    """Owns the worker thread; the GUI calls the instrument by emitting these.

    Each command signal is connected to the worker's slot of the same name.
    Because the worker lives on another thread the connection is queued, so
    emitting from a widget returns immediately and the blocking call happens on
    the worker thread.

    **The bridge outlives the worker.**  ``Dmm34461A`` takes its host in the
    constructor, so connecting to a different instrument means a complete
    teardown and rebuild — a new ``InstrumentWorker`` on a new ``QThread``.
    Everything the window connects to therefore lives here rather than on the
    worker: the command signals are re-pointed at the new worker and the
    worker's results are re-emitted from this object, so no widget connection
    ever has to be torn down and remade, and none can be left pointing at a
    worker that has gone.

    :meth:`connect_to` refuses to build a second worker while the first thread
    is still running, which is what keeps IO-DISCIPLINE.md rule 2 honest when
    the user switches meters: the previous instrument's handover has to have
    finished — trigger setup restored, ``SYST:LOC`` sent last, socket closed —
    before another link is opened.
    """

    # -------- commands: the GUI emits these, the worker's slots receive them
    refreshState = Signal()
    setFunction = Signal(str)
    setConfig = Signal(object)
    setTrigger = Signal(object)
    setMath = Signal(object)
    setDisplay = Signal(object)
    setStream = Signal(bool)
    single = Signal()
    clearStats = Signal()
    readStats = Signal()
    clearHistogram = Signal()
    readHistogram = Signal()
    setLog = Signal(bool, str)
    clearLog = Signal()
    exportLog = Signal(str)
    saveFrame = Signal(object)
    beep = Signal()
    selftest = Signal()
    lock = Signal(bool)
    reset = Signal()
    passthrough = Signal(str)
    readSystem = Signal()
    captureScreen = Signal()
    returnToLocal = Signal()
    proceedUnverified = Signal()

    # -------- results: re-emitted from the worker so the GUI has one sender
    stateReady = Signal(object)
    dataReady = Signal(object)
    readingReady = Signal(object)
    statsReady = Signal(object)
    limitReady = Signal(object)
    errorRaised = Signal(str)
    noticeRaised = Signal(str)
    systemReady = Signal(object)
    histogramReady = Signal(object)
    scpiReady = Signal(object)
    singleReady = Signal(object)
    selftestReady = Signal(object)
    frameReady = Signal(object, float)
    localReady = Signal(object)
    exportReady = Signal(str, int)
    linkChanged = Signal(bool, str)
    identityReady = Signal(object)
    openFailed = Signal(str)
    modelUnverified = Signal(object)

    #: The worker thread has ended and its instrument has been handed back.
    #: The window uses this both to finish closing and to learn that it may
    #: now build the next link.
    finished = Signal()

    _COMMANDS = (
        "refreshState",
        "setFunction",
        "setConfig",
        "setTrigger",
        "setMath",
        "setDisplay",
        "setStream",
        "single",
        "clearStats",
        "readStats",
        "clearHistogram",
        "readHistogram",
        "setLog",
        "clearLog",
        "exportLog",
        "saveFrame",
        "beep",
        "selftest",
        "lock",
        "reset",
        "passthrough",
        "readSystem",
        "captureScreen",
        "returnToLocal",
        "proceedUnverified",
    )

    _RESULTS = (
        "stateReady",
        "dataReady",
        "readingReady",
        "statsReady",
        "limitReady",
        "errorRaised",
        "noticeRaised",
        "systemReady",
        "histogramReady",
        "scpiReady",
        "singleReady",
        "selftestReady",
        "frameReady",
        "localReady",
        "exportReady",
        "linkChanged",
        "identityReady",
        "openFailed",
        "modelUnverified",
    )

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.thread: Optional[QThread] = None
        self.worker: Optional[InstrumentWorker] = None
        #: The address of the link this bridge currently owns, "" when none.
        self.host = ""
        self._stopping = False
        # A command emitted while there is no worker would otherwise vanish
        # without trace.  Every command signal carries this guard as well as
        # its worker connection, so a click that reaches nothing says so.
        for name in self._COMMANDS:
            getattr(self, name).connect(
                lambda *args, _name=name: self._unrouted(_name)
            )

    # --------------------------------------------------------------- lifecycle

    def is_running(self) -> bool:
        """True while a worker thread exists and has not finished."""
        return self.thread is not None and self.thread.isRunning()

    def connect_to(
        self,
        host: str,
        port: int = 5025,
        transport: str = "auto",
        force_finite_count: bool = False,
    ) -> Tuple[bool, str]:
        """Build and start a worker for *host*.  Returns (started, reason).

        Refuses while a previous worker thread is alive.  That is not
        defensive tidiness: the previous instrument is only handed back —
        acquisition stopped, trigger setup restored, ``SYST:LOC`` sent last —
        inside that thread's shutdown, and opening a second link before it has
        finished would leave the first meter in remote and acquiring.
        """
        if self.is_running():
            return False, (
                "the worker for the previous instrument is still running, so "
                "it has not finished handing that instrument back; no new "
                "link was opened"
            )
        self._release_worker()

        thread = QThread()
        thread.setObjectName("dmm-worker")
        worker = InstrumentWorker(host, port, transport, force_finite_count)
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        for name in self._COMMANDS:
            getattr(self, name).connect(getattr(worker, name), Qt.QueuedConnection)
        for name in self._RESULTS:
            getattr(worker, name).connect(getattr(self, name))
        # The worker ends its own event loop once shutdown() has finished.
        # Calling thread.quit() from outside instead would race: QThread::quit
        # ends the loop at its next iteration, which can happen before the
        # queued shutdown() call has been delivered — leaving the streaming
        # thread running and the link open.  Direct connection, so this runs on
        # the worker thread the moment shutdown() emits.
        worker.stopped.connect(thread.quit, Qt.DirectConnection)
        # The thread is bound into the connection, so the slot can tell its
        # own thread's notification from a stale one posted by a worker that
        # has already been replaced.  See _thread_finished.
        thread.finished.connect(lambda t=thread: self._thread_finished(t))

        self.thread = thread
        self.worker = worker
        self.host = host
        self._stopping = False
        thread.start()
        return True, ""

    def start(self) -> None:
        """Start the worker thread, for a bridge built by :meth:`connect_to`."""
        if self.thread is not None and not self.thread.isRunning():
            self.thread.start()

    def request_stop(self) -> bool:
        """Ask the worker to shut down, without waiting for it.

        For the GUI thread, which must stay responsive and must not run a
        nested event loop while the instrument is being handed back.  The
        caller watches :attr:`finished` to learn when it is done.  Returns
        False when there was nothing running to stop.
        """
        if self._stopping or not self.is_running():
            return False
        self._stopping = True
        # Queued, so it runs after anything already posted to the worker.  The
        # worker quits its own loop when it is done; see the connection above.
        QMetaObject.invokeMethod(self.worker, "shutdown", Qt.QueuedConnection)
        return True

    def stop(self, timeout_ms: int = 30000) -> bool:
        """Stop the run, join the worker and close the link, blocking.

        For callers that are *not* the GUI thread — a script driving the window
        headlessly, a test.  The window itself uses :meth:`request_stop`.
        """
        thread = self.thread
        if thread is None:
            return True
        if self._stopping:
            return not thread.isRunning() or bool(thread.wait(timeout_ms))
        if not self.request_stop():
            return True
        if thread.wait(timeout_ms):
            return True
        # It did not finish in time — most likely wedged in a socket read.
        # Ask the loop to end anyway and give it a short grace period, so the
        # caller learns the truth instead of blocking forever.
        thread.quit()
        return bool(thread.wait(2000))

    # ---------------------------------------------------------------- plumbing

    def _thread_finished(self, thread: QThread) -> None:
        """The worker thread *thread* has ended; drop it and tell the window.

        **The identity check is the whole point of this slot.**
        ``QThread::finished`` is delivered as a posted event, so it can arrive
        after this bridge has already released that worker and built the next
        one — :meth:`stop` joins the thread with ``wait()``, which does not
        pump the event loop, so the notification is still sitting in the queue
        when the next :meth:`connect_to` runs.  Releasing "whatever worker is
        current" on that event unhooked a **running** worker that still held
        an open link: every signal disconnected, ``request_stop()`` and
        ``is_running()`` answering False for ever — so ``closeEvent`` closed
        the window with the link open, no ``ABOR``, no trigger restore, no
        ``SYST:LOC``, while telling the user the instrument had been handed
        back — and ``deleteLater()`` queued against a running QThread, which
        Qt treats as fatal.

        The thread is bound into the connection in :meth:`connect_to` rather
        than read from ``sender()``, so the comparison holds however the slot
        is reached.
        """
        if thread is not self.thread:
            # A worker this bridge has already released; its successor is
            # live and must not be touched.
            return
        self._stopping = False
        self._release_worker()
        self.host = ""
        self.finished.emit()

    def _release_worker(self) -> None:
        """Unhook and drop the finished worker and its thread.

        Only ever called once the thread has stopped, so nothing is running
        that could still be delivered to the disconnected slots.  The command
        connections are removed explicitly rather than left to object
        destruction, so a command emitted between two links cannot be queued
        onto a worker that is on its way out.

        Every disconnect is individually guarded.  Anything raised here would
        otherwise escape :meth:`_thread_finished` — or, worse, ``shutdown``'s
        caller — and skip ``finished.emit()``, leaving a switch or a window
        close waiting out its 30 s watchdog with the link still open.
        """
        worker, thread = self.worker, self.thread
        self.worker = None
        self.thread = None
        if worker is not None:
            for name in self._COMMANDS:
                try:
                    getattr(self, name).disconnect(getattr(worker, name))
                except (RuntimeError, TypeError):
                    pass  # already gone; nothing is left pointing at it
            for name in self._RESULTS:
                try:
                    getattr(worker, name).disconnect(getattr(self, name))
                except (RuntimeError, TypeError):
                    pass
            # The worker owns the Dmm34461A and its log buffer — up to ~150 MB
            # — so it is dropped here and now rather than left to the cycle
            # collector.  Its QTimer children were already deleted on the
            # worker's own thread (see InstrumentWorker.shutdown), so nothing
            # with the dead thread's affinity is destroyed from this one.
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

    def _unrouted(self, name: str) -> None:
        """Report a command that arrived with no instrument link behind it."""
        if self.worker is None:
            self.errorRaised.emit(
                f"{name}: there is no instrument connected, so nothing was "
                f"sent. Use Connect to open a link first."
            )
