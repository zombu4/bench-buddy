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

"""Dmm34461A — the instrument model behind the desktop application.

**One** SCPI session, ``ctrl``, held open for the application's lifetime
(IO-DISCIPLINE.md rule 3).  The second ``view`` socket that used to exist
served only the continuous screen mirror; screen polling is withdrawn (rule 4),
so the link went with it.  The instrument supports two sessions, but fewer is
materially gentler on its Windows CE LAN stack, and we no longer need them.

Every command sent from this module appears in SPEC.md section 2.1.  The
never-send list of section 2.2 is enforced by :func:`app.scpi.check_allowed`,
which every call here passes through; only the user-typed SCPI console
passthrough deliberately bypasses it.

The instrument is never left holding a connection while idle.  Section
"acquisition" below owns the two modes rule 1 requires — a 3 Hz drained
free-running acquisition when idle, the ``INIT`` + ``R?`` drain when streaming
— and hands the trigger back to the front panel on the way out.
"""

from __future__ import annotations

import atexit
import contextlib
import csv
import io
import signal
import sys
import threading
import time
import weakref
from collections import deque
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .scpi import (
    LIMITER,
    ScpiConnectionError,
    ScpiError,
    ScpiLink,
    ScpiTimeout,
    boolean,
)
from .screen import ScreenDecodeError, bmp_to_png
from . import specs
from .specs import FUNCS, FunctionSpec

MAX_LOG_POINTS = 2_000_000
MAX_BLOCK_READINGS = 4000
PASSTHROUGH_TIMEOUT = 3.0
SELFTEST_TIMEOUT = 25.0
READ_TIMEOUT = 20.0
SCREEN_TIMEOUT = 15.0

# IO-DISCIPLINE.md rule 1: the idle keepalive runs at 2-4 Hz.  3 Hz sits in
# the middle of that band and costs 2 of the 40 operations per second the
# transport allows.
#
# WHAT IS DRAINED, AND WHY IT IS NOT A READ? HEARTBEAT
# ----------------------------------------------------
# Rule 1 prescribes a READ? heartbeat and forbids INIT with TRIG:COUN INF,
# because an undrained continuous acquisition fills the 1000-reading memory and
# raises -365.  Measured against the instrument, the prescription does not
# achieve the rule's own goal, and the reason for the prohibition disappears
# once the acquisition is drained.
#
# READ? is ABORt + INITiate + FETCh?, and the ABORt *blanks the displayed
# reading* for the whole of the next integration.  The panel only repaints
# about 100 ms after a reading completes, so at NPLC 10 (343 ms per reading)
# the digits are on screen for a sliver of each cycle.  Sampled from an
# independent socket while the keepalive ran, so the samples were not
# synchronised to it — what a person at the bench actually sees:
#
#     READ? + 120 ms gap        2.01 Hz   digits visible  2/10
#     READ? + 250 ms gap        1.67 Hz   digits visible  5/10
#     READ? + 400 ms gap        1.35 Hz   digits visible  5/10
#     INIT + R? 1 per beat      1.92 Hz   digits visible  3/10
#     TRIG:COUN INF, drained    4.57 Hz   digits visible 10/10, backlog 0
#
# Only a *continuous* acquisition keeps the panel lit, because the display
# holds the previous reading while the next one integrates; anything that ends
# and restarts an acquisition blanks it.  Draining that acquisition every beat
# keeps reading memory at zero — measured backlog 0 with an empty error queue —
# so the overflow the prohibition exists to prevent cannot occur.
#
# This therefore keeps rule 1's goal ("keep the front panel live") and its
# safety property ("without filling the 1000-reading memory") and departs only
# from the specific command it names.  See IO-DISCIPLINE.md rule 1 for the
# recorded measurements.
#
# WHY THE COUNT IS FINITE AND RENEWED, NOT INF
# --------------------------------------------
# TRIG:COUN INF makes the drain the only thing standing between the instrument
# and a full reading memory, and the drain lives in this process.  Kill the
# process — a hard kill, an unhandled Qt slot exception — and the acquisition
# keeps running instrument-side with nobody draining it.  Trigger state is
# global to the instrument, not per-session, so no atexit hook, excepthook or
# signal handler can be relied on to clean it up.
#
# The *crash* half of that is now handled underneath, by the transport: on a
# VXI-11 link the instrument destroys the link and device-clears itself about
# two seconds after the client's socket dies, whatever killed the client
# (app/scpi.py records the measurements).  What the transport cannot do is
# protect against an application that is still alive and simply not draining —
# a long passthrough, a 15 s screen capture, a wedged thread — because its link
# is still open and the instrument has no reason to intervene.  That case is
# what the finite renewed count below still exists for, and it is why the
# mechanism stays even though the link is now crash-safe.  On the raw-socket
# fallback it is also the only protection there is.
#
# A finite count is a deadman the *instrument* enforces.  Sized for roughly
# IDLE_ACQ_SECONDS of acquisition at the measured rate and clamped well under
# the 1000-reading memory, it expires by itself: a dead app, a 3 s passthrough
# stall or a 15 s screen capture all end with the acquisition simply running
# out instead of overflowing.  The drain renews it once half the count is used,
# so in normal operation it never expires.
#
# Renewal costs an ABOR/TRIG:COUN/INIT: measured against the instrument,
# TRIG:COUN cannot be written while an acquisition is in progress — it answers
# +263,"Not able to execute while instrument is measuring" and the old count
# stands.  So renewal blanks the display for one integration period, which is
# why it is done as rarely as the half-used rule allows.
HEARTBEAT_HZ = 3.0

# How long an idle acquisition should last before it has to be renewed, and
# the bounds on the resulting trigger count.  The lower bound keeps a slow
# function (NPLC 100 produces well under one reading a second) from renewing
# every beat; the upper bound keeps the deadman well under the 1000-reading
# memory even on the fastest function.
IDLE_ACQ_SECONDS = 2.0
IDLE_COUNT_MIN = 50
IDLE_COUNT_MAX = 500
# How long to wait for the keepalive thread to finish when stopping it.  A beat
# is a DATA:POIN? and an R?, neither of which blocks on a measurement, so this
# only has to cover an in-flight exchange.
HEARTBEAT_TIMEOUT = 10.0
# The quiet gap the front panel needs after a reading in order to paint it.
#
# Measured on the instrument, NPLC 10, where one READ? takes 343 ms:
#
#     gap    0 ms -> 3.0 Hz, display BLANK  (2252 lit pixels, never changing)
#     gap  100 ms -> 2.2 Hz, display LIVE  (~11000 lit pixels, changing)
#     gap  200 ms -> 2.0 Hz, display LIVE
#
# READ? is ABORt + INITiate + FETCh?, and the ABORt blanks the displayed
# reading.  Issued back to back there is never a moment in which the panel is
# not mid-abort, so it shows placeholder dashes for ever — the very "dead bench
# meter" this file exists to prevent, reached by a route that looks like it is
# working because the *application* is getting fresh values throughout.
# If this many readings are waiting, the drain has fallen behind and polls
# again at once instead of sleeping.  Reading memory holds 1000, so emptying it
# well before that is what keeps -365 structurally out of reach rather than
# merely unlikely.
IDLE_BACKLOG_HURRY = 200

# If no reading arrives for this long the continuous acquisition is not running
# — something ABORted it, a *RST, a single reading, a console command — so the
# keepalive re-initiates it.  Self-healing this way means every code path that
# stops an acquisition does not have to remember to tell the keepalive.
IDLE_RESTART_AFTER = 1.5

# How long the streaming drain waits before asking DATA:POIN? again.  Two
# operations per cycle (DATA:POIN? then R?) at 20 ms is 100/s, which the rate
# ceiling trims to 40/s — the drain is allowed to use the budget while a run is
# actually in progress, because the heartbeat is stood down and the state poll
# has dropped to 10 s.  When nothing is waiting, backing off to 100 ms keeps a
# slow function from costing anything like that.
STREAM_POLL_BUSY = 0.02
STREAM_POLL_IDLE = 0.1

# If a run produces nothing at all for this long its acquisition is not
# running — something ABORted it, or the INIT that should have resumed it after
# a configuration change never went out.  The slowest thing this instrument
# does is NPLC 100 with autozero, a little over 3 s a reading, so this is
# comfortably longer than any legitimate gap.
STREAM_RESTART_AFTER = 10.0

# The 34461A reports "no reading / overload" as 9.91E+37 wherever a measurement
# is expected: R? blocks, DATA:LAST?, READ?, the CALC:AVER: statistics and the
# CALC:TRAN:HIST: range read-backs.  It is a sentinel, never a measurement, and
# must never be published as a number.
OVERLOAD_SENTINEL = 9.9e37


def is_overload(value: Any) -> bool:
    """True when *value* is the instrument's 9.91E37 overload sentinel."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return abs(number) >= OVERLOAD_SENTINEL


def _no_overload(value: Optional[float]) -> Optional[float]:
    """Pass a measurement through, or ``None`` if it is the sentinel."""
    if value is None or is_overload(value):
        return None
    return value


class InstrumentError(ScpiError):
    """A request that the instrument or this model cannot honour."""


def _fmt(value: float) -> str:
    """Format a number for SCPI without losing precision to repr noise."""
    return f"{float(value):.9G}"


def _strip_quoted(command: str) -> str:
    """Remove the contents of quoted string arguments, quotes included."""
    out: List[str] = []
    quote = ""
    for char in command:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        out.append(char)
    return "".join(out)


def command_is_query(command: str) -> bool:
    """True when *command* asks the instrument for a response.

    A SCPI command is a query when its *mnemonic* ends in ``?``, not when a
    ``?`` appears anywhere in the line: ``DISP:TEXT "ready?"`` is a write, and
    treating it as a query costs a 3 s stall and a rebuilt link.
    """
    for piece in _strip_quoted(command).split(";"):
        head = piece.strip().split(" ")[0].split("\t")[0].strip()
        if head.endswith("?"):
            return True
    return False


class _AcquisitionPause:
    """Context manager that holds the idle heartbeat off the link.

    Entering increments the pause count, takes the instrument gate and stands
    the keepalive's own acquisition down, so nothing is left free-running with
    no drainer while the caller owns the link; leaving restarts it, releases
    the gate and wakes the heartbeat, so the pause costs one beat rather than a
    visible gap on the front panel.

    The counter is mutated under its own lock and every increment is paired
    with a decrement even when the gate acquire or the ``ABOR`` fails.  It used
    to be a bare ``+= 1`` outside any lock, incremented *before* the acquire:
    an interrupted acquire left it above zero for ever and the heartbeat never
    beat again.
    """

    def __init__(self, dmm: "Dmm34461A") -> None:
        self._dmm = dmm

    def __enter__(self) -> "Dmm34461A":
        dmm = self._dmm
        with dmm._pause_lock:
            dmm._paused += 1
        try:
            dmm._gate.acquire()
        except BaseException:
            with dmm._pause_lock:
                dmm._paused -= 1
            raise
        try:
            dmm._stop_idle_acquisition_locked()
        except BaseException:
            dmm._gate.release()
            with dmm._pause_lock:
                dmm._paused -= 1
            dmm._beat_wake.set()
            raise
        return dmm

    def __exit__(self, exc_type, exc, tb) -> bool:
        dmm = self._dmm
        # Drop the pause count first, while the gate is still held: the
        # restart below stands down again if the keepalive is still paused.
        with dmm._pause_lock:
            dmm._paused -= 1
        try:
            dmm._restart_idle_acquisition_locked()
        finally:
            dmm._gate.release()
            dmm._beat_wake.set()
        return False


# Every live instrument object, so the crash handlers installed by
# :func:`install_safety_net` can find them.  Weak, so an abandoned object is
# still collectable and a test that builds several does not leak.
_LIVE_INSTRUMENTS: "weakref.WeakSet[Dmm34461A]" = weakref.WeakSet()
_SAFETY_NET_LOCK = threading.Lock()
_SAFETY_NET_INSTALLED = False


class Dmm34461A:
    def __init__(
        self,
        host: str,
        port: int = 5025,
        transport: str = "auto",
        force_finite_count: bool = False,
    ):
        self.host = host
        self.port = port
        # Belt and braces: keep the idle keepalive's finite renewed trigger
        # count even on a transport that already ends the acquisition when
        # this process dies.  It costs one blanked sample in twelve on the
        # front panel, so it is off by default; see idle_deadman_needed().
        self.force_finite_count = force_finite_count
        # One link, for the application's lifetime (rule 3).  "auto" prefers
        # VXI-11, on which the instrument ends the acquisition by itself if
        # this process is killed, and falls back to the raw socket if the
        # instrument does not offer it.
        self.ctrl = ScpiLink(host, port, name="ctrl", timeout=10.0,
                             transport=transport)

        # Serialises multi-command sequences (abort/change/init) against the
        # streaming loop and the idle heartbeat, each of which grabs it once
        # per iteration.
        self._gate = threading.RLock()

        self.publish: Callable[[Dict[str, Any]], None] = lambda message: None
        self.last_error: Optional[str] = None

        self.identity: Dict[str, str] = {}
        self.ranges: Dict[str, List[float]] = {}
        # True once the range enumeration has run.  open() can stop between
        # *IDN? and the enumeration so a caller can decide whether this model
        # may be driven at all; finish_open() completes it afterwards.
        self._ranges_ready = False

        # False until open() has identified the instrument and enumerated its
        # ranges.  The server starts regardless and retries in the background,
        # so every read has to cope with this being False.
        self._opened = False
        self._open_lock = threading.Lock()
        self._last_state: Optional[Dict[str, Any]] = None

        self._overload = False
        self._memory_overflow = False

        self._streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_stop = threading.Event()
        self._trig_saved: Optional[Tuple[str, str, int]] = None
        self._stream_started_at = 0.0

        self._rate_events: deque = deque()
        self._rate_lock = threading.Lock()

        self._logging = False
        self._log_note = ""
        # An overloaded sample is stored with value None, never as 9.91E37.
        self._log: List[Tuple[float, Optional[float]]] = []
        self._log_lock = threading.Lock()
        self._log_overflow = False
        # How the logged readings are actually expressed, as a list of
        # ``(first row index, func, unit)`` segments.  A single frozen unit was
        # wrong in both directions: toggling CALC:SCAL:STAT during a recording
        # produced a file whose header and every row claimed the unit in force
        # when Record was pressed, and starting a second recording relabelled
        # the rows of the first.  The segments are appended to only when the
        # unit actually changes, so the cost is a few tuples per session.
        self._log_units: List[Tuple[int, str, str]] = []
        # The unit the *next* reading will be logged with, kept current
        # without any extra I/O: every state read refreshes it, and the two
        # operations that can change it out from under a running recording —
        # set_math's scaling fields and set_function — update it while they
        # still hold the gate, so not even the batch in flight is mislabelled.
        self._reading_func = ""
        self._reading_unit = ""

        # Rule 4: no capture thread and no timer.  The only screen data the
        # app ever holds is the last frame a user explicitly asked for.
        self._frame: Optional[bytes] = None
        self._frame_time = 0.0
        self._frame_lock = threading.Lock()
        self._screen_format_set = False

        # Rule 1: the idle keepalive.  Runs for the whole time the app is
        # connected and simply stands aside while the streaming loop owns the
        # acquisition, so there is no window in which neither is measuring.
        self._beat_thread: Optional[threading.Thread] = None
        self._beat_stop = threading.Event()
        self._beat_wake = threading.Event()
        self._beat_reason = ""
        self._beat_value: Optional[float] = None
        self._beat_time = 0.0
        self._beats = 0
        self._paused = 0
        self._pause_lock = threading.Lock()
        # Whether the keepalive may drive the trigger in the current setup, and
        # whether that verdict needs re-reading from the instrument.
        self._beat_safe = False
        self._beat_dirty = True
        # The keepalive's own acquisition (see HEARTBEAT_HZ).  On a transport
        # that ends the acquisition when this process dies it runs under
        # TRIG:COUN INF; otherwise under a finite count that the drain renews,
        # so it expires by itself if this process stops draining it.  See
        # :meth:`idle_deadman_needed`.
        self._idle_initiated = False
        self._idle_last_reading = 0.0
        self._idle_backlog = 0
        self._idle_count = 0
        # Whether the acquisition in force was started with TRIG:COUN INF.
        # Re-checked on every renewal, so a link rebuilt on a different
        # transport adopts the right strategy rather than keeping whichever it
        # happened to start with.
        self._idle_infinite = False
        self._idle_taken = 0
        self._idle_started_at = 0.0
        # Readings per second measured from the keepalive's own drain, which is
        # what sizes the trigger count.  None until it has been measured once.
        self._idle_rate: Optional[float] = None

        self._limit_status: Optional[str] = None
        _LIVE_INSTRUMENTS.add(self)

    # ------------------------------------------------------------- lifecycle

    def open(
        self,
        on_identity: Optional[Callable[[Dict[str, str]], bool]] = None,
        start_heartbeat: bool = True,
    ) -> None:
        """Connect the link, identify the instrument, enumerate ranges.

        Starts the idle heartbeat as the last step, so the application never
        holds a connection with the trigger system sitting idle (rule 1).

        *on_identity* is called with the parsed ``*IDN?`` **between** the
        identification and the range enumeration, and returning False stops
        the open there: the link stays up and the identity is known, but the
        ~24 ``<p>:RANG? MIN|MAX`` queries are not sent and the heartbeat is
        not started.  That is the seam the model guard needs (``ui/bridge.py``):
        on a model this command set was never verified against, those range
        queries are unsupported queries whose measured failure mode is a hung
        socket, and they used to run *before* the user was asked anything.
        Call :meth:`finish_open` to complete the open after the user accepts.

        The callback runs while the instrument gate is held, so it must not
        touch the link — the one this application passes is a dictionary
        lookup in ``app/models.py``.

        *start_heartbeat* False leaves the keepalive stopped for the caller to
        start.  The keepalive is a plain thread rather than a Qt object, so it
        is the one source of traffic no Qt-side gate can hold back; the caller
        holding a decision open has to hold this back with it.

        **A failed open deliberately does not close the link**, so that the
        caller can report the real error against a live socket.  The handback
        therefore depends on the caller tearing the worker down — in this
        application ``_on_open_failed`` always calls ``request_stop()``, whose
        ``close()`` sends ``ABOR`` and ``SYST:LOC``.  Anything else calling
        this must do the same or the instrument is left in remote.
        """
        with self._open_lock:
            enumerated = False
            try:
                self.ctrl.connect()
                with self._gate:
                    self.ctrl.write("*CLS")
                    idn = self.ctrl.query("*IDN?")
                    self.identity = self._parse_idn(idn)
                    self._ranges_ready = False
                    if on_identity is None or on_identity(dict(self.identity)):
                        self._enumerate_ranges()
                        self._ranges_ready = True
                        enumerated = True
            except ScpiError:
                self._opened = False
                raise
            self._opened = True
            # A fallback to the raw socket is not an error, but it withdraws
            # the instrument-enforced deadman, so it is stated rather than
            # left for someone to discover after a crash.
            self.last_error = self.ctrl.fallback_reason
            if enumerated and start_heartbeat:
                self.start_heartbeat()

    def finish_open(self, start_heartbeat: bool = True) -> None:
        """Complete an :meth:`open` that stopped at ``*IDN?``.

        Enumerates the ranges and starts the idle keepalive, in that order:
        the keepalive needs the function metadata, and rule 1 wants it running
        from the moment this application is driving the instrument.  Safe to
        call when the open already completed — the enumeration is not repeated.
        """
        if not self._opened:
            raise InstrumentError(
                "the link is not open, so there is nothing to finish opening"
            )
        if not self._ranges_ready:
            with self._gate:
                self._enumerate_ranges()
            self._ranges_ready = True
        if start_heartbeat:
            self.start_heartbeat()

    def try_open(
        self,
        on_identity: Optional[Callable[[Dict[str, str]], bool]] = None,
        start_heartbeat: bool = True,
    ) -> bool:
        """Attempt :meth:`open`, recording the failure instead of raising."""
        if self._opened:
            return True
        try:
            self.open(on_identity=on_identity, start_heartbeat=start_heartbeat)
        except (ScpiError, ValueError) as exc:
            self.last_error = f"cannot reach {self.host}:{self.port}: {exc}"
            return False
        return True

    @property
    def ranges_ready(self) -> bool:
        """True once the range enumeration of SPEC.md 2.4 has been done."""
        return self._ranges_ready

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def connected(self) -> bool:
        return self._opened and self.ctrl.connected

    def _mark_disconnected(self, exc: BaseException) -> None:
        """Record a lost link so the supervisor re-opens it."""
        self._opened = False
        self.last_error = f"link to {self.host}:{self.port} lost: {exc}"

    def _note_ok(self) -> None:
        """Clear the latched error after an operation completes cleanly."""
        self.last_error = None

    def live_workers(self) -> List[str]:
        """Names of worker threads that are still running."""
        alive: List[str] = []
        beat = self._beat_thread
        if beat is not None and beat.is_alive():
            alive.append("the idle keepalive")
        stream = self._stream_thread
        if stream is not None and stream.is_alive():
            alive.append("the streaming drain")
        return alive

    def close(self) -> Dict[str, Any]:
        """Shut down cleanly and hand the instrument back (rule 2).

        The order matters, and it is enforced rather than assumed.  Both of our
        acquisition modes are stopped first, and only once every worker thread
        is *confirmed* stopped is the user's trigger setup put back and the
        instrument returned to local — which is what makes the front panel
        free-run again instead of sitting frozen on our last reading.  Then the
        socket is closed.

        A thread that will not stop is reported, not worked around.  Sending
        ``SYST:LOC`` while a daemon thread is still on the link is worse than
        not sending it: the thread's next ``ABOR`` or ``DATA:POIN?`` puts the
        instrument straight back into remote, and in the keepalive's case
        leaves it remote, aborted and idle — the frozen panel of rule 2, handed
        back to the user with a success message on top of it.
        """
        problems: List[str] = []
        try:
            self.stop_heartbeat()
        except ScpiError as exc:
            problems.append(f"stopping the idle keepalive: {exc}")
        try:
            self.stop_stream()
        except ScpiError as exc:
            problems.append(f"stopping the run: {exc}")

        alive = self.live_workers()
        local: Dict[str, Any] = {"local": False, "detail": ""}
        if alive:
            detail = (
                " and ".join(alive)
                + " would not stop, so the instrument was left in remote "
                "rather than being handed back over a link another thread is "
                "still using. Press [Local] on the front panel."
            )
            problems.append(detail)
            local = {"local": False, "detail": detail}
        elif self.ctrl.connected:
            try:
                local = self.release()
            except ScpiError as exc:
                problems.append(f"handing the instrument back: {exc}")
                local = {"local": False, "detail": str(exc)}
        else:
            local = {"local": False, "detail": "the link was already closed"}

        self.ctrl.close()
        self._opened = False
        if problems:
            self.last_error = "; ".join(problems)
        return {"local": local, "problems": problems}

    def release(self) -> Dict[str, Any]:
        """ABOR, restore the user's trigger setup, return to local (rule 2).

        Verified on the instrument: leaving it ABORted and idle freezes the
        front panel on our last reading (0 readings taken in a silent 10 s
        window).  Sending ``SYST:LOC`` after restoring the trigger hands
        acquisition back to the front panel, which then free-runs on its own at
        ~2.9 rdg/s — with no ``TRIG:COUN INF`` acquisition of ours left running
        to fill reading memory.  That is why this restores *and* releases,
        rather than leaving an INIT behind to keep the display busy.
        """
        with self._gate:
            self._release_acquisition_locked()
            local = self._return_to_local_locked()
        self._note_ok()
        return local

    def _return_to_local_locked(self) -> Dict[str, Any]:
        """Send SYST:LOC as the last command on the link; caller holds ``_gate``.

        Probed once in isolation on a throwaway socket before ever being used
        from the application, as rule 6 demands: this instrument accepts it, it
        leaves no error queued, and the socket stays in sync afterwards.

        Nothing may be sent after it.  Any SCPI command over LAN puts the
        instrument straight back into remote, so a ``SYST:ERR?`` asking whether
        SYST:LOC worked is self-defeating — it undoes the very thing it is
        checking.  That was measured, not assumed: with the error check after
        it, the panel stayed idle and took no readings at all during a silent
        12 s window; with the check moved in front of it, the panel free-runs.
        So the error queue is drained *before*, leaving the queue clean, and
        anything SYST:LOC itself queues is reported by the next operation on a
        later connection.
        """
        try:
            before = self.errors(limit=5)
        except ScpiError as exc:
            before = [f"the queue could not be read first: {exc}"]
        try:
            self.ctrl.write("SYST:LOC", priority=True)
        except ScpiError as exc:
            # A genuine failure has to reach the user, who then has one
            # remaining route back: the front-panel [Local] key.
            self.last_error = f"SYST:LOC was not accepted: {exc}"
            return {"local": False, "detail": str(exc)}
        if before:
            message = "; ".join(before)
            self.last_error = (
                f"the error queue was not clean before returning to local: {message}"
            )
            return {"local": True, "detail": message}
        return {"local": True, "detail": ""}

    def return_to_local(self) -> Dict[str, Any]:
        """The UI's explicit "Return to Local" action.

        Stops our acquisition, restores the user's trigger setup and releases
        the instrument, exactly as shutdown does — the difference is only that
        the socket stays open afterwards.  The heartbeat is left stopped,
        because resuming it would immediately put the instrument back into
        remote and undo what the user just asked for.

        **Nothing is sent after this returns.**  ``SYST:LOC`` is only the last
        command on the link if the caller makes it so: this used to publish a
        fresh state immediately afterwards, ~33 queries that put the meter back
        into remote within milliseconds and left it remote *and* idle, because
        the keepalive had just been stopped — the exact fault the button
        exists to cure, reported as a success.  The worker also has to stop its
        periodic state poll; see ``ui/bridge.py``.
        """
        try:
            self.stop_heartbeat()
        except ScpiError as exc:
            self.last_error = f"the idle keepalive would not stop: {exc}"
        if self._streaming:
            try:
                self.stop_stream()
            except ScpiError as exc:
                self.last_error = f"the run would not stop: {exc}"
        # The condition that actually matters is that nothing is left on the
        # link, not whether the stop call reported success.
        alive = self.live_workers()
        if alive:
            detail = " and ".join(alive) + (
                " is still using the link, so SYST:LOC was not sent — it would "
                "have been undone by the next command that thread sends. Press "
                "[Local] on the front panel."
            )
            self.last_error = detail
            self._beat_reason = detail
            return {"local": False, "detail": detail}
        result = self.release()
        self._beat_reason = (
            "the instrument was returned to local; the app is not driving it"
        )
        return result

    def local_state(self) -> Dict[str, Any]:
        """The last known state, marked as handed back, with no I/O at all.

        After :meth:`return_to_local` the UI still has to be told what
        happened, and reading the real state would cost ~33 queries — each of
        which puts the instrument straight back into remote.  Everything here
        is already held in this process.
        """
        state = dict(self._last_state or self._offline_state())
        state["ranges"] = list(state.get("ranges") or [])
        state["connected"] = self.ctrl.connected
        state["local"] = True
        state["error"] = self.last_error
        state["streaming"] = False
        state["logging"] = self._logging
        state["log_count"] = self.log_count()
        state["log_overflow"] = self._log_overflow
        state["rate_hz"] = 0.0
        state["overload"] = self._overload
        state["memory_overflow"] = self._memory_overflow
        state["heartbeat"] = self.heartbeat_state()
        state["capture_time"] = self.capture_time()
        state["scpi_rate"] = LIMITER.rate()
        return state

    @staticmethod
    def _parse_idn(idn: str) -> Dict[str, str]:
        parts = [p.strip() for p in idn.split(",")]
        while len(parts) < 4:
            parts.append("")
        return {
            "idn": idn,
            "vendor": parts[0],
            "model": parts[1],
            "serial": parts[2],
            "firmware": parts[3],
        }

    def _enumerate_ranges(self) -> None:
        """Build every range list from the instrument (SPEC.md section 2.4)."""
        self.ranges = {}
        for node in specs.RANGE_NODES:
            low = self.ctrl.query_float(f"{node}:RANG? MIN")
            high = self.ctrl.query_float(f"{node}:RANG? MAX")
            self.ranges[node] = specs.enumerate_ranges(low, high)

    # ------------------------------------------------------------- utilities

    @contextlib.contextmanager
    def _hold(self, may_change_trigger: bool = False) -> Iterator[None]:
        """Own the link with the keepalive's acquisition stood down.

        Every operation that keeps ``_gate`` for longer than a single exchange
        goes through here, uniformly.  While the gate is held the keepalive
        cannot drain, so an acquisition left free-running behind it piles
        readings into the instrument's memory with nobody taking them out — a
        33-query state read, a 30-query system read, a 15 s screen capture or a
        3 s console stall were each long enough to do it.

        The acquisition is restarted on the way out rather than left to the
        next beat, so the window in which neither mode is measuring is the
        operation's own duration and not a beat period longer (rule 1).

        *may_change_trigger* says the body can move the trigger system —
        ``CONF:``, ``*RST``, a trigger edit, a user-typed command.  Restarting
        after one of those would write ``TRIG:SOUR IMM`` over a BUS or EXT
        source the user had just asked for, so the restart is left to the next
        beat, which re-reads the source before deciding anything.
        """
        with self._gate:
            self._stop_idle_acquisition_locked()
            try:
                yield
            finally:
                if may_change_trigger:
                    self.note_setup_changed()
                else:
                    self._restart_idle_acquisition_locked()

    def _restart_idle_acquisition_locked(self) -> None:
        """Put the keepalive's acquisition back after a hold; holds ``_gate``.

        Best effort by design: a failure here is recorded and published, never
        raised, because raising would mask the result of whatever the caller
        was actually doing and the keepalive re-initiates by itself within
        :data:`IDLE_RESTART_AFTER` anyway.
        """
        if self._streaming or self._idle_initiated:
            return
        if not self.heartbeat_running or not self._beat_safe:
            return
        if self._beat_stop.is_set() or self._paused:
            return
        if self._beat_dirty:
            # The trigger source has to be re-read before the keepalive may
            # drive anything; the next beat does that and starts it properly.
            return
        try:
            self._start_idle_acquisition_locked()
        except ScpiError as exc:
            self.last_error = f"restarting the idle acquisition: {exc}"
            self.publish({"type": "error", "message": self.last_error})

    def _pause_for_change(self) -> bool:
        """Abort an in-flight acquisition so configuration can be changed.

        The instrument rejects configuration writes with ``+263,"Not able to
        execute while instrument is measuring"`` whenever *anything* is
        acquiring, and a rejected write is silent on the wire — the setting
        simply does not change.  The idle keepalive is an acquisition too, so
        it has to stand down here exactly as a run does.  It needs no matching
        resume: it re-initiates by itself on its next beat.
        """
        self._stop_idle_acquisition_locked()
        if self._streaming:
            self.ctrl.write("ABOR", priority=True)
            return True
        return False

    def _resume_after_change(self, was_running: bool) -> None:
        """Put the streaming acquisition back after a configuration change.

        Always reached through a ``finally``: a validation failure part-way
        through a batch of writes must not be able to leave the instrument
        aborted mid-reconfigure, measuring nothing, while the UI still says
        "Running".  Switching to 4-wire resistance with a queued autozero write
        did exactly that — FRES has no autozero, so the write raised after the
        ``ABOR`` had already gone out.
        """
        if not was_running:
            return
        self.ctrl.write("TRIG:SOUR IMM")
        self.ctrl.write("TRIG:COUN INF")
        self.ctrl.write("SAMP:COUN 1")
        self.ctrl.write("INIT")

    def current_func(self) -> FunctionSpec:
        raw = self.ctrl.query("SENS:FUNC?")
        key = specs.resolve_sense_func(raw)
        if key is None or key not in FUNCS:
            raise InstrumentError(f"unrecognised measurement function {raw!r}")
        return FUNCS[key]

    def _query_measurement(self, command: str) -> float:
        """Read a value that the instrument may decorate with its unit.

        ``DATA:LAST?`` answers e.g. ``+2.55526268E-04  VDC``.
        """
        raw = self.ctrl.query(command).strip()
        token = raw.split()[0] if raw.split() else ""
        try:
            return float(token)
        except ValueError as exc:
            raise InstrumentError(
                f"{command}: expected a measurement, got {raw!r}"
            ) from exc

    def errors(self, limit: int = 20) -> List[str]:
        """Drain the SCPI error queue."""
        found: List[str] = []
        for _ in range(limit):
            raw = self.ctrl.query("SYST:ERR?")
            if raw.startswith("+0,") or raw.startswith("0,"):
                break
            found.append(raw)
        return found

    # ----------------------------------------------------------------- state

    def read_state(self) -> Dict[str, Any]:
        """The State object of SPEC.md section 4.1.

        Never raises: an unreachable instrument degrades to the last known
        state with ``connected: false`` and the real error, so the UI and the
        background reconnect supervisor both keep working.
        """
        if not self._opened:
            return self._offline_state()
        try:
            state = self._read_state_online()
        except (ScpiConnectionError, ScpiTimeout) as exc:
            if isinstance(exc, ScpiTimeout) and not exc.link_reset:
                # VXI-11: the RPC completed and the link is still up, so the
                # session is not lost.  Marking it disconnected would show the
                # user "disconnected" over one slow query and send the
                # supervisor through a pointless re-open — *CLS, *IDN? and a
                # full range enumeration — against an instrument that is
                # already busy enough to have missed a deadline.
                self.last_error = f"state poll: {exc}"
                state = self._offline_state()
                if self._last_state is not None:
                    state["connected"] = True
                return state
            self._mark_disconnected(exc)
            return self._offline_state()
        self._last_state = state
        # Free of charge: this read already knows the function and the scaling
        # state, so it is also what keeps the log's labelling current when
        # something outside this application — the front panel, the SCPI
        # console — changes either one.
        func, unit = self._unit_from_state(state)
        self._note_reading_unit(func, unit)
        return state

    def _offline_state(self) -> Dict[str, Any]:
        """A full-shaped State object describing a link that is not up."""
        if self._last_state is not None:
            state = dict(self._last_state)
            state["ranges"] = list(state.get("ranges") or [])
        else:
            spec = FUNCS["VOLT:DC"]
            state = {
                "func": spec.key,
                "func_label": spec.label,
                "short": spec.short,
                "unit": spec.unit,
                "ranges": [],
                "range": None,
                "range_auto": None,
                "resolution": None,
                "nplc": None,
                "nplc_options": list(specs.NPLC_OPTIONS),
                "aperture": None,
                "aperture_options": [],
                "azero": None,
                "impedance": None,
                "band": None,
                "band_options": [],
                "temp": None,
                "trigger": {
                    "source": None,
                    "delay": None,
                    "delay_auto": None,
                    "count": None,
                    "samples": None,
                    "slope": None,
                },
                "math": {
                    "null_on": None,
                    "null_value": None,
                    "null_auto": None,
                    "scale_on": None,
                    "scale_func": None,
                    "db_ref": None,
                    "dbm_ref": None,
                    "stats_on": None,
                    "limit_on": None,
                    "limit_low": None,
                    "limit_high": None,
                    "hist_on": None,
                    "hist_points": None,
                    "hist_auto": None,
                    "hist_low": None,
                    "hist_high": None,
                },
                "display": {"view": None, "on": None, "text": None},
            }
        state["connected"] = False
        state["local"] = False
        state["error"] = self.last_error or f"not connected to {self.host}:{self.port}"
        state["streaming"] = self._streaming
        state["logging"] = self._logging
        state["log_count"] = self.log_count()
        state["log_overflow"] = self._log_overflow
        state["rate_hz"] = self.rate_hz()
        state["overload"] = self._overload
        state["memory_overflow"] = self._memory_overflow
        state["heartbeat"] = self.heartbeat_state()
        state["capture_time"] = self.capture_time()
        state["scpi_rate"] = LIMITER.rate()
        return state

    def _read_state_online(self) -> Dict[str, Any]:
        with self._hold():
            spec = self.current_func()
            state: Dict[str, Any] = {
                "connected": self.ctrl.connected,
                "transport": self.ctrl.transport_name,
                "crash_safe": self.ctrl.crash_safe,
                "transport_note": self.ctrl.fallback_reason,
                "local": False,
                "error": self.last_error,
                "func": spec.key,
                "func_label": spec.label,
                "short": spec.short,
                "unit": spec.unit,
                "ranges": list(self.ranges.get(spec.range_node or "", [])),
                "range": None,
                "range_auto": None,
                "resolution": None,
                "nplc": None,
                # SPEC.md section 4.1 shows nplc_options unconditionally; the
                # per-function applicability is carried by `nplc` being null.
                "nplc_options": list(specs.NPLC_OPTIONS),
                "aperture": None,
                "aperture_options": (
                    list(specs.APERTURE_OPTIONS) if spec.aperture_node else []
                ),
                "azero": None,
                "impedance": None,
                "band": None,
                "band_options": list(specs.BAND_OPTIONS) if spec.band else [],
                "temp": None,
            }

            if spec.range_node:
                state["range"] = self.ctrl.query_float(f"{spec.range_node}:RANG?")
                state["range_auto"] = self.ctrl.query_bool(
                    f"{spec.range_node}:RANG:AUTO?"
                )
            if spec.has_resolution:
                state["resolution"] = self.ctrl.query_float(f"{spec.sense}:RES?")
            if spec.nplc:
                state["nplc"] = self.ctrl.query_float(f"{spec.sense}:NPLC?")
            if spec.aperture_node:
                state["aperture"] = self.ctrl.query_float(f"{spec.aperture_node}:APER?")
            elif spec.key == "VOLT:DC":
                # VOLT:DC:APER? is the only integration-time read-back in 2.1.
                state["aperture"] = self.ctrl.query_float("VOLT:DC:APER?")
            if spec.azero:
                state["azero"] = self._read_azero(spec)
            if spec.impedance:
                hiz = self.ctrl.query_bool(f"{spec.sense}:IMP:AUTO?")
                state["impedance"] = "HIZ" if hiz else "10M"
            if spec.band:
                state["band"] = self.ctrl.query_float(f"{spec.sense}:BAND?")
            if spec.temperature:
                state["temp"] = {
                    "type": self.ctrl.query("TEMP:TRAN:TYPE?"),
                    "unit": self.ctrl.query("UNIT:TEMP?"),
                    "rtd_res": self.ctrl.query_float("TEMP:TRAN:RTD:RES?"),
                    "therm_type": self.ctrl.query("TEMP:TRAN:THER:TYPE?").lstrip("+"),
                    "types": list(specs.TEMP_PROBE_TYPES),
                    "units": list(specs.TEMP_UNITS),
                }

            state["trigger"] = self._read_trigger()
            state["math"] = self._read_math(spec)
            state["display"] = {
                "view": self.ctrl.query("DISP:VIEW?"),
                "on": self.ctrl.query_bool("DISP?"),
                "text": self.ctrl.query_str("DISP:TEXT?"),
            }

        with self._log_lock:
            log_count = len(self._log)
            log_overflow = self._log_overflow
        state["streaming"] = self._streaming
        state["logging"] = self._logging
        state["log_count"] = log_count
        state["log_overflow"] = log_overflow
        state["rate_hz"] = self.rate_hz()
        state["overload"] = self._overload
        state["memory_overflow"] = self._memory_overflow
        state["heartbeat"] = self.heartbeat_state()
        state["capture_time"] = self.capture_time()
        state["scpi_rate"] = LIMITER.rate()
        return state

    def capture_time(self) -> Optional[float]:
        """Epoch at which the user last captured the screen, or None.

        A capture is a snapshot of a moment, so the UI labels it with when it
        was taken rather than implying it is current.
        """
        with self._frame_lock:
            if self._frame is None or self._frame_time <= 0.0:
                return None
            return self._frame_time

    def heartbeat_state(self) -> Dict[str, Any]:
        """What the idle keepalive is doing, for the UI to show honestly."""
        return {
            "running": self.heartbeat_running,
            "hz": HEARTBEAT_HZ,
            "beats": self._beats,
            "beating": bool(
                self.heartbeat_running and self._beat_safe and not self._streaming
            ),
            "reason": self._beat_reason,
            "value": self._beat_value,
            "at": self._beat_time or None,
            # Which thing would stop the acquisition if this process died.
            # "instrument" means the transport does it; "trigger count" means
            # the finite renewed count below is the only protection there is.
            "deadman": "trigger count" if self.idle_deadman_needed() else "instrument",
            "count": "INF" if self._idle_infinite else self._idle_count,
            "forced_finite": self.force_finite_count,
        }

    def _read_azero(self, spec: FunctionSpec) -> str:
        raw = self.ctrl.query(f"{spec.sense}:ZERO:AUTO?").strip().upper()
        if raw in ("1", "+1"):
            return "ON"
        if raw in ("0", "+0"):
            return "OFF"
        return raw

    def _read_trigger(self) -> Dict[str, Any]:
        raw_count = self.ctrl.query("TRIG:COUN?")
        try:
            count_value = float(raw_count)
            count: Any = (
                "INF" if count_value >= 9e37 else int(round(count_value))
            )
        except ValueError:
            count = raw_count.strip().upper()
        trigger: Dict[str, Any] = {
            "source": self.ctrl.query("TRIG:SOUR?"),
            "delay": self.ctrl.query_float("TRIG:DEL?"),
            "delay_auto": self.ctrl.query_bool("TRIG:DEL:AUTO?"),
            "count": count,
            "samples": self.ctrl.query_int("SAMP:COUN?"),
            "slope": self.ctrl.query("TRIG:SLOP?"),
        }
        # While either acquisition mode owns the trigger, the count and sample
        # count read back are ours, not the user's: the keepalive's deadman
        # count and its SAMP:COUN 1, or the run's INF.  Reporting those would
        # tell a user who had just set Count = 10 that their setting had
        # reverted on its own, within one state poll and with no explanation.
        # What is held in _trig_saved is both what they asked for and what the
        # release will put back, so that is what the UI is shown.
        #
        # The *source* is not overlaid while idle.  The keepalive only ever
        # writes IMM when the source is already IMM — it stands down for BUS
        # and EXT — so the read-back is the truth, and overlaying it would
        # report a stale IMM after something outside set_trigger (the SCPI
        # console, the front panel) had moved it.  A run does force the source,
        # so there it is overlaid.
        saved = self._trig_saved
        if saved is not None:
            source, saved_count, samples = saved
            if self._streaming:
                trigger["source"] = source
            token = saved_count.strip().upper()
            if token in ("INF", "INFINITY"):
                trigger["count"] = "INF"
            else:
                try:
                    trigger["count"] = int(float(token))
                except ValueError:
                    trigger["count"] = token
            trigger["samples"] = samples
            trigger["held_by_app"] = True
        else:
            trigger["held_by_app"] = False
        return trigger

    def _read_math(self, spec: FunctionSpec) -> Dict[str, Any]:
        math: Dict[str, Any] = {
            "null_on": None,
            "null_value": None,
            "null_auto": None,
            "scale_on": self.ctrl.query_bool("CALC:SCAL:STAT?"),
            "scale_func": self.ctrl.query("CALC:SCAL:FUNC?"),
            "db_ref": self.ctrl.query_float("CALC:SCAL:DB:REF?"),
            "dbm_ref": self.ctrl.query_float("CALC:SCAL:DBM:REF?"),
            "stats_on": self.ctrl.query_bool("CALC:AVER:STAT?"),
            "limit_on": self.ctrl.query_bool("CALC:LIM:STAT?"),
            "limit_low": self.ctrl.query_float("CALC:LIM:LOW?"),
            "limit_high": self.ctrl.query_float("CALC:LIM:UPP?"),
            "hist_on": self.ctrl.query_bool("CALC:TRAN:HIST:STAT?"),
            "hist_points": self.ctrl.query_int("CALC:TRAN:HIST:POIN?"),
            "hist_auto": self.ctrl.query_bool("CALC:TRAN:HIST:RANG:AUTO?"),
            # On auto-range with no data yet the instrument answers 9.91E37;
            # that is "not determined", not a bound the UI may draw.
            "hist_low": _no_overload(
                self.ctrl.query_float("CALC:TRAN:HIST:RANG:LOW?")
            ),
            "hist_high": _no_overload(
                self.ctrl.query_float("CALC:TRAN:HIST:RANG:UPP?")
            ),
        }
        if spec.has_null:
            math["null_on"] = self.ctrl.query_bool(f"{spec.sense}:NULL:STAT?")
            math["null_value"] = self.ctrl.query_float(f"{spec.sense}:NULL:VAL?")
            math["null_auto"] = self.ctrl.query_bool(f"{spec.sense}:NULL:VAL:AUTO?")
        return math

    def system_info(self) -> Dict[str, Any]:
        with self._hold():
            cal_year, cal_month, cal_day = self.ctrl.query_floats("CAL:DATE?")
            date_parts = self.ctrl.query_floats("SYST:DATE?")
            uptime = [int(v) for v in self.ctrl.query_floats("SYST:UPT?")]
            info = {
                "idn": self.identity.get("idn", ""),
                "vendor": self.identity.get("vendor", ""),
                "model": self.identity.get("model", ""),
                "serial": self.identity.get("serial", ""),
                "firmware": self.identity.get("firmware", ""),
                "lan": {
                    "hostname": self.ctrl.query_str("SYST:COMM:LAN:HOSTname?"),
                    "ip": self.ctrl.query_str("SYST:COMM:LAN:IPADdress?"),
                    "mac": self.ctrl.query_str("SYST:COMM:LAN:MAC?"),
                    "dhcp": self.ctrl.query_bool("SYST:COMM:LAN:DHCP?"),
                    "subnet": self.ctrl.query_str("SYST:COMM:LAN:SMAS?"),
                    "gateway": self.ctrl.query_str("SYST:COMM:LAN:GAT?"),
                    "dns": self.ctrl.query_str("SYST:COMM:LAN:DNS?"),
                    "domain": self.ctrl.query_str("SYST:COMM:LAN:DOM?"),
                    "telnet_welcome": self.ctrl.query_str("SYST:COMM:LAN:TELN:WMES?"),
                    "lxi_identify": self.ctrl.query_bool("LXI:IDEN:STAT?"),
                },
                "cal": {
                    "count": self.ctrl.query_int("CAL:COUN?"),
                    "date": "%04d-%02d-%02d"
                    % (int(cal_year), int(cal_month), int(cal_day)),
                    "string": self.ctrl.query_str("CAL:STR?"),
                },
                "uptime": {
                    "days": uptime[0] if len(uptime) > 0 else 0,
                    "hours": uptime[1] if len(uptime) > 1 else 0,
                    "minutes": uptime[2] if len(uptime) > 2 else 0,
                    "seconds": uptime[3] if len(uptime) > 3 else 0,
                    "text": ("%dd %02d:%02d:%02d" % tuple(uptime[:4]))
                    if len(uptime) >= 4
                    else "",
                },
                "lfr": self.ctrl.query_float("SYST:LFR?"),
                "terminals": self.ctrl.query("ROUT:TERM?"),
                "options": self.ctrl.query("*OPT?"),
                "date": "%04d-%02d-%02d"
                % tuple(int(v) for v in (date_parts + [0, 0, 0])[:3]),
                "time": self.ctrl.query("SYST:TIME?"),
                "secure_count": self.ctrl.query("SYST:SEC:COUN?"),
                "beeper": self.ctrl.query_bool("SYST:BEEP:STAT?"),
                "click": self.ctrl.query_bool("SYST:CLIC:STAT?"),
                "lock_owner": self.ctrl.query("SYST:LOCK:OWN?"),
                "questionable": self.ctrl.query_int("STAT:QUES:COND?"),
                "operation": self.ctrl.query_int("STAT:OPER:COND?"),
                "esr": self.ctrl.query_int("*ESR?"),
                "stb": self.ctrl.query_int("*STB?"),
                "host": self.host,
                "port": self.port,
                "errors": self.errors(),
            }
        self._note_ok()
        return info

    # ------------------------------------------------------------ configure

    def set_function(self, key: str) -> None:
        if key not in FUNCS:
            raise InstrumentError(
                f"unknown function {key!r}; expected one of {', '.join(FUNCS)}"
            )
        spec = FUNCS[key]
        with self._hold(may_change_trigger=True):
            running = self._pause_for_change()
            try:
                self.ctrl.write(f"CONF:{spec.conf}")
            finally:
                self._resume_after_change(running)
            # Still holding the gate: the readings after this point are in the
            # new function's unit, and a recording in progress must not label
            # them with the old one.  CONF: does not change the scaling state,
            # so only the base unit moves.
            self._note_reading_unit(
                spec.key,
                "dB" if self._reading_unit == "dB" else
                "dBm" if self._reading_unit == "dBm" else spec.unit,
            )
        self._limit_status = None
        self._overload = False
        # CONF: resets the trigger system to IMM / 1 / 1.
        self.note_setup_changed()
        self._note_ok()

    def set_config(self, changes: Dict[str, Any]) -> None:
        if not changes:
            raise InstrumentError("no configuration fields supplied")
        with self._hold():
            spec = self.current_func()
            running = self._pause_for_change()
            try:
                for field, value in changes.items():
                    if value is None:
                        continue
                    self._apply_config_field(spec, field, value)
            finally:
                self._resume_after_change(running)
        self._note_ok()

    def _apply_config_field(self, spec: FunctionSpec, field: str, value: Any) -> None:
        if field == "range":
            if not spec.range_node:
                raise InstrumentError(f"{spec.label} has no range setting")
            self.ctrl.write(f"{spec.range_node}:RANG {_fmt(value)}")
        elif field == "range_auto":
            if not spec.range_node:
                raise InstrumentError(f"{spec.label} has no range setting")
            self.ctrl.write(f"{spec.range_node}:RANG:AUTO {boolean(value)}")
        elif field == "nplc":
            if not spec.nplc:
                raise InstrumentError(f"{spec.label} has no NPLC setting")
            nplc = float(value)
            if nplc not in specs.NPLC_OPTIONS:
                raise InstrumentError(
                    f"NPLC {nplc:g} is not one of "
                    + ", ".join(f"{v:g}" for v in specs.NPLC_OPTIONS)
                )
            self.ctrl.write(f"{spec.sense}:NPLC {_fmt(nplc)}")
        elif field == "azero":
            if not spec.azero:
                raise InstrumentError(f"{spec.label} has no autozero setting")
            token = str(value).strip().upper()
            if token in ("1", "TRUE"):
                token = "ON"
            elif token in ("0", "FALSE"):
                token = "OFF"
            if token not in specs.AZERO_OPTIONS:
                raise InstrumentError(
                    f"autozero must be ON, OFF or ONCE (got {value!r})"
                )
            self.ctrl.write(f"{spec.sense}:ZERO:AUTO {token}")
        elif field == "impedance":
            if not spec.impedance:
                raise InstrumentError(f"{spec.label} has no input impedance setting")
            token = str(value).strip().upper()
            if token in ("HIZ", "HI", "AUTO", "1", "TRUE", "10G", ">10G"):
                arg = "ON"
            elif token in ("10M", "0", "FALSE", "10E6"):
                arg = "OFF"
            else:
                raise InstrumentError(
                    f"impedance must be '10M' or 'HIZ' (got {value!r})"
                )
            self.ctrl.write(f"{spec.sense}:IMP:AUTO {arg}")
        elif field == "band":
            if not spec.band:
                raise InstrumentError(f"{spec.label} has no AC bandwidth setting")
            band = int(float(value))
            if band not in specs.BAND_OPTIONS:
                raise InstrumentError(
                    "AC bandwidth must be 3, 20 or 200 Hz (got %r)" % (value,)
                )
            self.ctrl.write(f"{spec.sense}:BAND {band}")
        elif field == "aperture":
            if not spec.aperture_node:
                raise InstrumentError(f"{spec.label} has no aperture setting")
            aperture = float(value)
            if aperture not in specs.APERTURE_OPTIONS:
                raise InstrumentError(
                    "aperture must be one of "
                    + ", ".join(f"{v:g}" for v in specs.APERTURE_OPTIONS)
                )
            self.ctrl.write(f"{spec.aperture_node}:APER {_fmt(aperture)}")
        elif field == "temp_type":
            self._require_temp(spec)
            token = str(value).strip().upper()
            if token not in specs.TEMP_PROBE_TYPES:
                raise InstrumentError(
                    "probe type must be one of " + ", ".join(specs.TEMP_PROBE_TYPES)
                )
            self.ctrl.write(f"TEMP:TRAN:TYPE {token}")
        elif field == "temp_unit":
            self._require_temp(spec)
            token = str(value).strip().upper()
            if token not in specs.TEMP_UNITS:
                raise InstrumentError("temperature unit must be C, F or K")
            self.ctrl.write(f"UNIT:TEMP {token}")
        elif field == "rtd_res":
            self._require_temp(spec)
            self.ctrl.write(f"TEMP:TRAN:RTD:RES {_fmt(value)}")
        elif field == "therm_type":
            self._require_temp(spec)
            token = str(int(float(value)))
            if token not in specs.THERMISTOR_TYPES:
                raise InstrumentError("thermistor type must be 5000")
            self.ctrl.write(f"TEMP:TRAN:THER:TYPE {token}")
        else:
            raise InstrumentError(f"unknown configuration field {field!r}")

    @staticmethod
    def _require_temp(spec: FunctionSpec) -> None:
        if not spec.temperature:
            raise InstrumentError(
                "probe settings apply to the temperature function only"
            )

    def set_trigger(self, changes: Dict[str, Any]) -> None:
        if not changes:
            raise InstrumentError("no trigger fields supplied")
        with self._hold(may_change_trigger=True):
            running = self._pause_for_change()
            try:
                self._apply_trigger_fields(changes)
            finally:
                self._resume_after_change(running)
        self.note_setup_changed()
        self._note_ok()

    def _apply_trigger_fields(self, changes: Dict[str, Any]) -> None:
        """Write one batch of trigger changes; caller holds ``_gate``."""
        for field, value in changes.items():
            if value is None:
                continue
            if field == "source":
                token = str(value).strip().upper()
                if token not in specs.TRIG_SOURCES:
                    raise InstrumentError("trigger source must be IMM, BUS or EXT")
                self.ctrl.write(f"TRIG:SOUR {token}")
            elif field == "delay":
                self.ctrl.write(f"TRIG:DEL {_fmt(value)}")
            elif field == "delay_auto":
                self.ctrl.write(f"TRIG:DEL:AUTO {boolean(value)}")
            elif field == "count":
                token = str(value).strip().upper()
                if token in ("INF", "INFINITY"):
                    self.ctrl.write("TRIG:COUN INF")
                else:
                    self.ctrl.write(f"TRIG:COUN {int(float(value))}")
            elif field == "samples":
                self.ctrl.write(f"SAMP:COUN {int(float(value))}")
            elif field == "slope":
                token = str(value).strip().upper()
                if token not in specs.TRIG_SLOPES:
                    raise InstrumentError("trigger slope must be POS or NEG")
                self.ctrl.write(f"TRIG:SLOP {token}")
            else:
                raise InstrumentError(f"unknown trigger field {field!r}")
        if self._streaming or self._trig_saved is not None:
            # Whichever mode is driving owns source/count/samples, so the
            # user's choice has to be remembered for the release.
            #
            # It must be merged from what was *asked for*, not re-read from the
            # instrument: ABOR does not undo TRIG:COUN, so while either mode
            # owns the trigger a read-back still reports the count this code
            # put there, and capturing it would record the keepalive's own
            # deadman count as though the user had chosen it — which the
            # release would then faithfully restore.
            self._trig_saved = self._merge_saved_trigger(changes)

    def _merge_saved_trigger(self, changes: Dict[str, Any]) -> Tuple[str, str, int]:
        """Fold a user's trigger edit into the setup kept for the release."""
        source, count, samples = self._trig_saved or self._capture_trigger()
        if changes.get("source") is not None:
            source = str(changes["source"]).strip().upper()
        if changes.get("count") is not None:
            token = str(changes["count"]).strip().upper()
            count = "INF" if token in ("INF", "INFINITY") else str(
                int(float(changes["count"]))
            )
        if changes.get("samples") is not None:
            samples = int(float(changes["samples"]))
        return source, count, samples

    def _capture_trigger(self) -> Tuple[str, str, int]:
        source = self.ctrl.query("TRIG:SOUR?")
        raw_count = self.ctrl.query("TRIG:COUN?")
        try:
            count = "INF" if float(raw_count) >= 9e37 else str(int(float(raw_count)))
        except ValueError:
            count = raw_count.strip()
        samples = self.ctrl.query_int("SAMP:COUN?")
        return source, count, samples

    def set_math(self, changes: Dict[str, Any]) -> None:
        if not changes:
            raise InstrumentError("no math fields supplied")
        with self._hold():
            spec = self.current_func()
            running = self._pause_for_change()
            try:
                for field, value in changes.items():
                    if value is None:
                        continue
                    self._apply_math_field(spec, field, value)
            finally:
                self._resume_after_change(running)
            if "scale_on" in changes or "scale_func" in changes:
                # Two queries, still under the gate, so that not even the
                # batch already in flight can be logged under the old label.
                # The instrument is asked what it adopted rather than told
                # what was requested — it does not always take what it is
                # given (SPEC.md 2.1).
                self._note_reading_unit(
                    spec.key, self._read_reading_unit_locked(spec)
                )
        self._limit_status = None
        self._note_ok()

    def _apply_math_field(self, spec: FunctionSpec, field: str, value: Any) -> None:
        if field in ("null_on", "null_value", "null_auto"):
            if not spec.has_null:
                raise InstrumentError(f"{spec.label} has no null setting")
            if field == "null_on":
                self.ctrl.write(f"{spec.sense}:NULL:STAT {boolean(value)}")
            elif field == "null_value":
                self.ctrl.write(f"{spec.sense}:NULL:VAL {_fmt(value)}")
            else:
                self.ctrl.write(f"{spec.sense}:NULL:VAL:AUTO {boolean(value)}")
        elif field == "scale_on":
            self.ctrl.write(f"CALC:SCAL:STAT {boolean(value)}")
        elif field == "scale_func":
            token = str(value).strip().upper()
            if token == "NULL":
                # Measured 2026-08-27 on this unit (34461A, firmware A.03.03),
                # error queue drained first, scaling both on and off:
                #   CALC:SCAL:FUNC DB   -> readback DB,  +0,"No error"
                #   CALC:SCAL:FUNC DBM  -> readback DBM, +0,"No error"
                #   CALC:SCAL:FUNC NULL -> readback DBM, -224,"Illegal parameter value"
                # SPEC.md section 2.1 lists NULL as an accepted value; that is
                # right for the query and wrong for the write.  Sending it
                # anyway would put -224 in the instrument's error queue for a
                # command that cannot work, so it is refused here with the
                # reason instead.  Null is reached through the per-function
                # <p>:NULL:STAT / <p>:NULL:VAL controls, which do work.
                raise InstrumentError(
                    "this instrument rejects CALC:SCAL:FUNC NULL with "
                    '-224,"Illegal parameter value" — use the per-function '
                    "NULL control instead; CALC:SCAL:FUNC accepts only DB "
                    "and DBM here"
                )
            if token not in specs.SCALE_FUNCS:
                raise InstrumentError(
                    f"scale function must be one of "
                    f"{', '.join(specs.SCALE_FUNCS)}, not {token!r}"
                )
            self.ctrl.write(f"CALC:SCAL:FUNC {token}")
        elif field == "db_ref":
            self.ctrl.write(f"CALC:SCAL:DB:REF {_fmt(value)}")
        elif field == "dbm_ref":
            self.ctrl.write(f"CALC:SCAL:DBM:REF {_fmt(value)}")
        elif field == "stats_on":
            self.ctrl.write(f"CALC:AVER:STAT {boolean(value)}")
        elif field == "limit_on":
            self.ctrl.write(f"CALC:LIM:STAT {boolean(value)}")
        elif field == "limit_low":
            self.ctrl.write(f"CALC:LIM:LOW {_fmt(value)}")
        elif field == "limit_high":
            self.ctrl.write(f"CALC:LIM:UPP {_fmt(value)}")
        elif field == "hist_on":
            self.ctrl.write(f"CALC:TRAN:HIST:STAT {boolean(value)}")
        elif field == "hist_points":
            self.ctrl.write(f"CALC:TRAN:HIST:POIN {int(float(value))}")
        elif field == "hist_auto":
            self.ctrl.write(f"CALC:TRAN:HIST:RANG:AUTO {boolean(value)}")
        elif field == "hist_low":
            self.ctrl.write(f"CALC:TRAN:HIST:RANG:LOW {_fmt(value)}")
        elif field == "hist_high":
            self.ctrl.write(f"CALC:TRAN:HIST:RANG:UPP {_fmt(value)}")
        else:
            raise InstrumentError(f"unknown math field {field!r}")

    def set_display(self, changes: Dict[str, Any]) -> None:
        if not changes:
            raise InstrumentError("no display fields supplied")
        with self._hold():
            if changes.get("view") is not None:
                token = str(changes["view"]).strip().upper()
                if token not in specs.DISPLAY_VIEWS:
                    raise InstrumentError(
                        "display view must be one of " + ", ".join(specs.DISPLAY_VIEWS)
                    )
                self.ctrl.write(f"DISP:VIEW {token}")
            if changes.get("on") is not None:
                self.ctrl.write(f"DISP {boolean(changes['on'])}")
            if "text" in changes and changes["text"] is not None:
                text = str(changes["text"])
                if '"' in text:
                    raise InstrumentError("display text may not contain a quote mark")
                if len(text) > 40:
                    raise InstrumentError("display text is limited to 40 characters")
                if text == "":
                    self.ctrl.write("DISP:TEXT:CLE")
                else:
                    self.ctrl.write(f'DISP:TEXT "{text}"')
        self._note_ok()

    # -------------------------------------------------------------- readings

    def single(self) -> Dict[str, Any]:
        """Take exactly one reading with ``READ?``, then restore the setup."""
        with self._hold():
            spec = self.current_func()
            was_streaming = self._streaming
            # _hold() has already stood the keepalive's acquisition down.
            # Capturing the trigger while it ran would save the keepalive's own
            # deadman count and then faithfully restore that as though the user
            # had chosen it; the user's real setup is already held in
            # _trig_saved, and the acquisition is restarted on the way out.
            self.ctrl.write("ABOR", priority=True)
            saved = self._capture_trigger()
            forced = saved != ("IMM", "1", 1)
            # READ? only returns if the acquisition terminates: force a single
            # immediate reading, whatever the user's trigger setup is.
            try:
                if forced:
                    self.ctrl.write("TRIG:SOUR IMM")
                    self.ctrl.write("TRIG:COUN 1")
                    self.ctrl.write("SAMP:COUN 1")
                value = self.ctrl.query_float("READ?", timeout=READ_TIMEOUT)
            finally:
                # A READ? that times out must not leave the trigger system
                # forced to IMM/1/1 with the user's own setup discarded.
                if forced:
                    self.ctrl.write(f"TRIG:SOUR {saved[0]}", priority=True)
                    self.ctrl.write(f"TRIG:COUN {saved[1]}", priority=True)
                    self.ctrl.write(f"SAMP:COUN {saved[2]}", priority=True)
                self._resume_after_change(was_streaming)
        overload = is_overload(value)
        self._overload = overload
        self._note_ok()
        return {
            "value": None if overload else value,
            "unit": spec.unit,
            "func": spec.key,
            "overload": overload,
        }

    # ------------------------------------------------------ idle heartbeat

    @property
    def heartbeat_running(self) -> bool:
        thread = self._beat_thread
        return thread is not None and thread.is_alive()

    @property
    def heartbeat_reason(self) -> str:
        """Why the heartbeat is not beating, or "" when it is."""
        return self._beat_reason

    def start_heartbeat(self) -> None:
        """Begin the 2-4 Hz idle keepalive (rule 1).

        Started when the link opens and left running for the whole session.
        It stands aside whenever the streaming loop owns the acquisition, so
        the two modes hand over without a window in which neither is measuring.
        """
        if self.heartbeat_running:
            return
        self._beat_stop.clear()
        self._beat_wake.clear()
        self._beat_dirty = True
        self._beat_thread = threading.Thread(
            target=self._beat_loop, name="dmm-heartbeat", daemon=True
        )
        self._beat_thread.start()

    def stop_heartbeat(self) -> None:
        self._beat_stop.set()
        self._beat_wake.set()
        thread = self._beat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=HEARTBEAT_TIMEOUT + 2.0)
            if thread.is_alive():
                raise InstrumentError(
                    "the idle heartbeat did not stop within "
                    f"{HEARTBEAT_TIMEOUT + 2.0:g} s; it still holds the link"
                )
        self._beat_thread = None
        self._beat_reason = "stopped"

    def note_setup_changed(self) -> None:
        """Re-check whether a plain ``READ?`` is still safe to send.

        Called after anything that can move the trigger system.  The check
        itself costs three queries, so it is done once after a change rather
        than on every beat.
        """
        self._beat_dirty = True
        self._beat_wake.set()

    def _heartbeat_safety_locked(self) -> Tuple[bool, str]:
        """May the keepalive drive the trigger right now?  Caller holds ``_gate``.

        Only the trigger *source* is checked.  The keepalive owns the trigger
        count and sample count while it runs, exactly as streaming does, and
        hands the user's own values back at release; but a BUS or EXT source is
        a deliberate instruction to wait for something the application cannot
        supply, so it is left alone rather than forced to IMM.  A panel holding
        its reading under an external trigger is the instrument doing as it was
        told, not the fault this file exists to fix, and the reason is put in
        the state so the UI says which of the two it is.
        """
        source = self.ctrl.query("TRIG:SOUR?").strip().upper()
        if not source.startswith("IMM"):
            return False, (
                f"the trigger source is {source}, so the instrument is waiting "
                "for a trigger and holds its last reading until one arrives"
            )
        return True, ""

    def _readings_from_block(self, body: bytes) -> List[float]:
        """Parse an ``R?`` block, treating a malformed one as a framing loss.

        ``parse_block_floats`` raises :class:`ValueError`, which neither
        acquisition loop expects; one bad block used to kill the thread outright
        and leave the acquisition running with nobody draining it.  A block that
        is not comma-separated floats also means the byte stream is out of step,
        so the link is rebuilt here as SPEC.md section 3 requires, and the
        failure is re-raised as the :class:`ScpiError` both loops already handle.
        """
        try:
            return ScpiLink.parse_block_floats(body)
        except ValueError as exc:
            preview = body[:40]
            detail = ""
            try:
                self.ctrl.reconnect()
            except ScpiConnectionError as rebuild_exc:
                detail = f"; the link is not back up yet: {rebuild_exc}"
            raise ScpiError(
                f"a reading block was not numeric ({preview!r}); the byte "
                f"stream was out of step and the link was rebuilt{detail}"
            ) from exc

    def _measure_idle_rate_locked(self) -> None:
        """Update the measured reading rate that sizes the trigger count."""
        if self._idle_taken <= 0:
            return
        elapsed = time.monotonic() - self._idle_started_at
        if elapsed < 0.2:
            return
        observed = self._idle_taken / elapsed
        if not self._idle_infinite and self._idle_taken >= self._idle_count:
            # The count ran out part-way through this window, so the elapsed
            # time includes a stretch in which nothing was measuring: this is a
            # lower bound on the rate, not a measurement of it.  Taking it only
            # when it is higher than what we hold is what lets the count grow
            # back after a change to a much faster function, without letting an
            # expiry shrink the count and make itself permanent.
            if self._idle_rate is None or observed > self._idle_rate:
                self._idle_rate = observed
            return
        self._idle_rate = observed

    def _renew_idle_acquisition_locked(self) -> None:
        """Re-arm the deadman.  Caller holds ``_gate``.

        Renewed once half the count has been used: late enough to keep the
        ABOR/INIT — and the one blanked integration it costs the front panel —
        as rare as the deadman allows, and early enough that the acquisition
        never actually runs out while the app is alive.

        Also renewed when the count in force is at least twice what the
        measured rate now calls for.  Without that the count seeded before any
        measurement would stand until half of it had been used, which on a slow
        function is minutes: the deadman would be far longer than the two
        seconds it is meant to be, purely because the acquisition it is
        protecting is a slow one.
        """
        if not self._idle_initiated:
            return
        # The transport can change underneath a live session — a rebuilt link
        # that fell back to the raw socket, or one that got VXI-11 back.  This
        # is the point at which that is noticed, so the acquisition adopts the
        # strategy the link now calls for instead of keeping the one it was
        # started with.  Restarting costs the same ABOR/INIT a renewal does.
        if self._idle_infinite == self.idle_deadman_needed():
            self._start_idle_acquisition_locked()
            return
        if self._idle_infinite:
            # Nothing to renew: the instrument holds the deadman, and leaving
            # the acquisition unbroken is exactly what keeps the panel lit.
            return
        if self._idle_count <= 0:
            return
        halfway = self._idle_taken * 2 >= self._idle_count
        oversized = self.idle_trigger_count() * 2 <= self._idle_count
        if not halfway and not oversized:
            return
        self._start_idle_acquisition_locked()

    def idle_deadman_needed(self) -> bool:
        """Whether the keepalive has to carry its own deadman.

        Read from the transport **actually in force**, never from what was
        asked for on the command line: an ``auto`` run that fell back to the
        raw socket must get the finite count, and so must a link that is
        momentarily down, because :attr:`ScpiLink.crash_safe` is False until a
        crash-safe session is genuinely up.  Erring towards the finite count
        while the answer is unknown is the safe direction.

        On a VXI-11 link the instrument destroys the link and device-clears
        itself about two seconds after this process stops existing, whatever
        killed it, so a deadman of ours would only duplicate that — at a cost
        of one blanked sample in twelve on the front panel, every ~8.6 s,
        because TRIG:COUN cannot be rewritten while a measurement is running
        and so each renewal must ABOR first.  See IO-DISCIPLINE.md rule 1.

        ``force_finite_count`` overrides all of it, for anyone who wants the
        belt as well as the braces.
        """
        return self.force_finite_count or not self.ctrl.crash_safe

    def idle_trigger_count(self) -> int:
        """How many readings the next idle acquisition is allowed to take.

        Sized for :data:`IDLE_ACQ_SECONDS` at the rate this keepalive has
        actually measured, clamped to [:data:`IDLE_COUNT_MIN`,
        :data:`IDLE_COUNT_MAX`].  The clamp is the safety property: whatever
        the rate estimate says, the instrument can never take more than
        IDLE_COUNT_MAX readings without this process asking it to, and that is
        half of the 1000-reading memory.

        With no measurement yet the top of the clamp is used rather than the
        bottom.  500 is the memory-safe end, and it cannot expire between two
        beats even on the fastest function, so the panel is not blinked while
        the rate is still being learned; the first renewal, well under a second
        later, replaces it with the measured size.
        """
        rate = self._idle_rate
        if rate is None or rate <= 0.0:
            return IDLE_COUNT_MAX
        wanted = int(round(rate * IDLE_ACQ_SECONDS))
        return max(IDLE_COUNT_MIN, min(IDLE_COUNT_MAX, wanted))

    def _start_idle_acquisition_locked(self) -> None:
        """Put the instrument into a finite acquisition.  Holds ``_gate``.

        The user's trigger setup is saved on the way in if nothing has saved it
        already, so a later release can put it back.  ``_trig_saved`` is shared
        with streaming and captured only once, which is what lets the two modes
        hand over without either of them mistaking the other's configuration
        for the user's.

        Which count is used depends on the transport in force
        (:meth:`idle_deadman_needed`).  On a crash-safe link it is
        ``TRIG:COUN INF``, because the instrument already ends the acquisition
        by itself if this process dies, and an unbroken acquisition is what
        keeps the front panel lit — 12 samples in 12 rather than 11.
        Otherwise the count is finite and renewed by the drain, which is then
        the only thing standing between a dead application and an instrument
        left acquiring for ever.

        The ``ABOR`` goes out before the decision so that the decision is made
        against a link that is genuinely up: the write forces a connect, and
        ``crash_safe`` is only meaningful once a session exists.
        """
        if self._trig_saved is None:
            self._trig_saved = self._capture_trigger()
        self.ctrl.write("ABOR", priority=True)
        self.ctrl.write("TRIG:SOUR IMM")
        infinite = not self.idle_deadman_needed()
        count = 0 if infinite else self.idle_trigger_count()
        self.ctrl.write("TRIG:COUN INF" if infinite else f"TRIG:COUN {count}")
        self.ctrl.write("SAMP:COUN 1")
        self.ctrl.write("INIT")
        self._idle_initiated = True
        self._idle_infinite = infinite
        self._idle_count = count
        self._idle_taken = 0
        self._idle_started_at = time.monotonic()
        self._idle_last_reading = self._idle_started_at

    def _stop_idle_acquisition_locked(self) -> None:
        """ABORt the keepalive's acquisition.  Caller holds ``_gate``.

        Always called before the keepalive lets go of the link.  The finite
        count means a forgotten acquisition expires rather than overflowing,
        but ending it deliberately is still what keeps reading memory empty and
        the instrument's state predictable for the next caller.
        """
        self._idle_backlog = 0
        self._idle_taken = 0
        self._idle_count = 0
        self._idle_infinite = False
        if not self._idle_initiated:
            return
        self._idle_initiated = False
        self.ctrl.write("ABOR", priority=True)

    def _beat_once(self) -> bool:
        """Drain one beat's worth of readings.  True when any arrived.

        Every exit resets ``_idle_backlog``.  It used to be left at whatever
        the last draining beat set it to, so after one beat that drained 200 or
        more the loop's "hurry" branch stayed latched: on a quiet beat that
        meant polling ``DATA:POIN?`` at the full rate ceiling, and on a beat
        that returned early — a BUS or EXT trigger source, a run starting —
        a bare ``continue`` with no I/O and no sleep at all, spinning one core
        at 100% for the rest of the session.
        """
        self._idle_backlog = 0
        with self._gate:
            if self._streaming or self._paused or self._beat_stop.is_set():
                return False
            if self._beat_dirty:
                safe, reason = self._heartbeat_safety_locked()
                self._beat_dirty = False
                self._beat_safe = safe
                self._beat_reason = reason
                if not safe:
                    self._stop_idle_acquisition_locked()
            if not self._beat_safe:
                return False

            now_m = time.monotonic()
            if not self._idle_initiated or (
                now_m - self._idle_last_reading > IDLE_RESTART_AFTER
            ):
                # Either we have not started yet, or something else ABORted the
                # acquisition out from under us.  Either way, (re)start it.
                self._start_idle_acquisition_locked()

            available = self.ctrl.query_int("DATA:POIN?")
            if available <= 0:
                self._renew_idle_acquisition_locked()
                return False
            want = min(available, MAX_BLOCK_READINGS)
            body = self.ctrl.query_block(f"R? {want}")
            self._idle_last_reading = time.monotonic()
            self._idle_backlog = available
            self._beat_reason = ""
            values = self._readings_from_block(body)
            self._idle_taken += len(values)
            self._measure_idle_rate_locked()
            self._renew_idle_acquisition_locked()

        if not values:
            return False

        value = values[-1]
        overload = is_overload(value)
        now = time.time()
        self._overload = overload
        self._beat_value = None if overload else float(value)
        self._beat_time = now
        self._beats += 1
        with self._rate_lock:
            self._rate_events.append((time.monotonic(), len(values)))
            cutoff = time.monotonic() - 1.0
            while self._rate_events and self._rate_events[0][0] < cutoff:
                self._rate_events.popleft()
        # A distinct message type: an idle beat keeps the readout live but is
        # not part of a run, so it must not be pushed into the chart or the log.
        self.publish(
            {
                "type": "reading",
                "t": now,
                "v": None if overload else float(value),
                "ovld": overload,
            }
        )
        return True

    def _beat_loop(self) -> None:
        """The idle keepalive.

        The cleanup is in a ``finally``: it used to sit after the ``while`` at
        function-body level, so anything the loop did not catch — a malformed
        block raising :class:`ValueError`, for one — killed the thread with an
        acquisition still running and nobody draining it.
        """
        period = 1.0 / HEARTBEAT_HZ
        failures = 0
        try:
            while not self._beat_stop.is_set():
                started = time.monotonic()
                if self._streaming or self._paused:
                    # The streaming loop is measuring, so the panel is already
                    # live; wait to be woken rather than polling the instrument.
                    self._beat_reason = "the run is measuring"
                    self._beat_wake.wait(0.05)
                    self._beat_wake.clear()
                    continue
                try:
                    drained = self._beat_once()
                    failures = 0
                except (ScpiTimeout, ScpiConnectionError) as exc:
                    failures += 1
                    self.last_error = f"idle keepalive: {exc}"
                    self.publish(
                        {"type": "error", "message": f"idle keepalive: {exc}"}
                    )
                    if isinstance(exc, ScpiTimeout) and not exc.link_reset:
                        # VXI-11: the timeout arrived as a completed RPC, so
                        # the link is still up and our acquisition is still
                        # running and still ours to drain.  Declaring the
                        # session lost would be false, and the ABOR below would
                        # blank the front panel — the very fault rule 1 exists
                        # to prevent — to recover from a non-event.  Treat it
                        # as an ordinary failed beat and try again.
                        if failures >= 3:
                            self._beat_reason = (
                                f"stopped after three failures: {exc}"
                            )
                            break
                        self._beat_stop.wait(1.0)
                        continue
                    self._mark_disconnected(exc)
                    # Do not sleep for up to 5 s with our own acquisition still
                    # running: nothing would be draining it for the whole of
                    # the backoff.  The finite count bounds that anyway, but
                    # ending it deliberately is what keeps memory empty.
                    self._abandon_idle_acquisition(exc)
                    # The supervisor re-opens the link; back off meanwhile so a
                    # dead instrument is not polled three times a second.
                    self._beat_stop.wait(min(1.0 * failures, 5.0))
                    continue
                except (ScpiError, ValueError) as exc:
                    failures += 1
                    self.last_error = f"idle keepalive: {exc}"
                    self.publish({"type": "error", "message": self.last_error})
                    if failures >= 3:
                        self._beat_reason = f"stopped after three failures: {exc}"
                        break
                    self._beat_stop.wait(1.0)
                    continue
                if drained and self._idle_backlog >= IDLE_BACKLOG_HURRY:
                    # Behind on a fast function: drain again straight away
                    # rather than let reading memory climb.  Guarded by
                    # ``drained`` so this branch can only be taken by an
                    # iteration that actually did I/O — an iteration that
                    # neither sleeps nor talks to the instrument is a spin.
                    continue
                elapsed = time.monotonic() - started
                self._beat_stop.wait(max(0.005, period - elapsed))
        finally:
            # Never leave an acquisition behind with nobody draining it.
            try:
                with self._gate:
                    self._stop_idle_acquisition_locked()
            except ScpiError as exc:
                self.last_error = f"stopping the idle acquisition: {exc}"
            self._beat_stop.set()

    def _abandon_idle_acquisition(self, cause: BaseException) -> None:
        """End our acquisition after a link failure, best effort.

        The link has just been rebuilt by :class:`ScpiLink` (or is waiting out
        its backoff), so this may well fail; what it must not do is leave
        ``_idle_initiated`` claiming an acquisition that nobody is draining.
        """
        self._idle_initiated = False
        self._idle_backlog = 0
        self._idle_taken = 0
        self._idle_count = 0
        try:
            with self._gate:
                self.ctrl.write("ABOR", priority=True)
        except ScpiError as exc:
            self.last_error = (
                f"idle keepalive: {cause}; the acquisition could not be "
                f"aborted afterwards either: {exc}"
            )

    def pause_acquisition(self) -> "_AcquisitionPause":
        """Hold the heartbeat and the streaming drain off the link briefly.

        Rule 4 asks a one-shot screen grab to pause the acquisition around
        itself rather than open a second socket for it.
        """
        return _AcquisitionPause(self)

    # ------------------------------------------------------------- streaming

    @property
    def streaming(self) -> bool:
        return self._streaming

    def start_stream(self) -> None:
        # The whole start sequence runs under the gate, so a concurrent
        # stop_stream() cannot land between the configuration and the thread
        # start and leave the caller with a silently dead run.
        with self._gate:
            if self._streaming:
                return
            stale = self._stream_thread
            if stale is not None and stale.is_alive():
                raise InstrumentError(
                    "the previous streaming thread is still shutting down; "
                    "two readers would split the R? blocks between them"
                )
            # The keepalive may already hold the user's setup; capturing again
            # here would save its own TRIG:COUN INF as though the user had
            # chosen it.  Capture only when nobody has.
            self._stop_idle_acquisition_locked()
            if self._trig_saved is None:
                self._trig_saved = self._capture_trigger()
            self.ctrl.write("ABOR")
            self.ctrl.write("*CLS")
            self.ctrl.write("TRIG:SOUR IMM")
            self.ctrl.write("TRIG:COUN INF")
            self.ctrl.write("SAMP:COUN 1")
            self.ctrl.write("INIT")
            self._streaming = True
            self._stream_started_at = time.time()
            self._limit_status = None
            self._overload = False
            self._memory_overflow = False
            with self._rate_lock:
                self._rate_events.clear()
            self._stream_stop.clear()
            self._stream_thread = threading.Thread(
                target=self._stream_loop, name="dmm-stream", daemon=True
            )
            self._stream_thread.start()
        # The heartbeat sees _streaming under the gate and stands aside; the
        # trigger setup it cached is stale either way.
        self.note_setup_changed()
        self._note_ok()

    def stop_stream(self) -> None:
        thread = self._stream_thread
        self._stream_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            if thread.is_alive():
                raise InstrumentError(
                    "the streaming thread did not stop within 5 s; "
                    "the run is still holding the instrument"
                )
        self._stream_thread = None
        with self._gate:
            if not self._streaming and self._trig_saved is None:
                return
            self._streaming = False
            self._restore_trigger_locked()
        # Rule 1: the handover must not leave a window in which neither mode
        # is measuring, so the heartbeat is re-checked and woken immediately
        # rather than waiting out its next period.
        self.note_setup_changed()
        self._note_ok()

    def _release_acquisition_locked(self) -> None:
        """Stop whichever mode is driving, then restore the user's setup."""
        self._stop_idle_acquisition_locked()
        self._restore_trigger_locked()

    def _restore_trigger_locked(self) -> None:
        """Abort the acquisition and put the user's trigger setup back.

        The caller must hold ``_gate``.  ``_trig_saved`` is consumed exactly
        once, so no later start_stream() can mistake the streaming
        configuration (IMM/INF/1) for the user's own — but only *after* the
        restore has actually gone out.

        It used to be cleared first.  ``abort_stream`` is called precisely when
        the link has just failed, so the ``ABOR`` raising is the expected case,
        not the exotic one; the saved setup was then gone, the meter was left
        on the acquisition configuration, and the next idle acquisition
        captured *that* as though the user had chosen it — which shutdown then
        faithfully restored.
        """
        saved = self._trig_saved
        self.ctrl.write("ABOR", priority=True)
        if saved is not None:
            source, count, samples = saved
            self.ctrl.write(f"TRIG:SOUR {source}", priority=True)
            self.ctrl.write(f"TRIG:COUN {count}", priority=True)
            self.ctrl.write(f"SAMP:COUN {samples}", priority=True)
        self._trig_saved = None

    def abort_stream(self, reason: str) -> None:
        """Stop a run from inside a worker thread or a failed request.

        Unlike :meth:`stop_stream` this never joins the streaming thread — it
        may be called *by* that thread — but it still sends ``ABOR``, restores
        the trigger setup and broadcasts a fresh state, as SPEC.md section 4.2
        requires after any change.
        """
        with self._gate:
            if not self._streaming and self._trig_saved is None:
                return
            self._streaming = False
            self._stream_stop.set()
            try:
                self._restore_trigger_locked()
            except ScpiError as exc:
                self.last_error = (
                    f"{reason}; the trigger setup could not be restored: {exc}"
                )
                self.publish({"type": "error", "message": self.last_error})
        self.note_setup_changed()
        self.publish_state()

    def publish_state(self) -> None:
        """Broadcast the current State object to every WebSocket client.

        Called from worker threads, so a failure to read the state is reported
        in band rather than being allowed to kill the caller.
        """
        try:
            state = self.read_state()
        except ScpiError as exc:
            self.last_error = f"could not read the instrument state: {exc}"
            self.publish({"type": "error", "message": self.last_error})
            return
        self.publish({"type": "state", "state": state})

    def _stream_loop(self) -> None:
        """The streaming drain.

        The ``ABOR`` cleanup is in a ``finally`` and covers every way out of
        this function, not just the ones the loop anticipated.  It used to sit
        after the ``while`` at function-body level and the loop caught only
        :class:`ScpiError`, so a malformed ``R?`` block — which raises
        :class:`ValueError` — killed the thread outright with ``TRIG:COUN INF``
        still acquiring and nothing draining it, while ``_streaming`` stayed
        True so the keepalive went on standing aside and the UI went on saying
        "Running".
        """
        last_stats = 0.0
        consecutive_failures = 0
        last_readings_at = time.monotonic()
        stop_reason: Optional[str] = None
        left_loop = False
        try:
            while not self._stream_stop.is_set():
                try:
                    with self._gate:
                        if not self._streaming:
                            break
                        available = self.ctrl.query_int("DATA:POIN?")
                        body = b""
                        if available > 0:
                            want = min(available, MAX_BLOCK_READINGS)
                            body = self.ctrl.query_block(f"R? {want}")
                        elif (
                            time.monotonic() - last_readings_at
                            > STREAM_RESTART_AFTER
                        ):
                            # Nothing has arrived for long enough that the
                            # acquisition cannot still be running: a *RST, a
                            # console ABOR, or a configuration change whose
                            # INIT did not make it back out.  Left alone the
                            # loop would poll DATA:POIN? -> 0 for ever while
                            # the instrument measured nothing and the UI said
                            # "Running".
                            self._restart_stream_acquisition_locked()
                            last_readings_at = time.monotonic()
                    if body:
                        values = self._readings_from_block(body)
                        if values:
                            last_readings_at = time.monotonic()
                            self._on_readings(values)

                    # Pace the drain against how much is actually waiting.  The
                    # old 5 ms spin issued DATA:POIN? as fast as the link
                    # allowed: at NPLC 10, where the instrument produces about
                    # 4 readings a second, it spent the entire 40 operations
                    # per second the transport allows on polling to collect
                    # them, starving every other caller and loading the LAN
                    # stack for nothing.
                    if available >= MAX_BLOCK_READINGS // 2:
                        # Falling behind — reading memory is finite, so drain
                        # again immediately rather than risk a -365 overflow.
                        wait = 0.0
                    elif body:
                        wait = STREAM_POLL_BUSY
                    else:
                        wait = STREAM_POLL_IDLE
                    if wait:
                        self._stream_stop.wait(wait)

                    now = time.monotonic()
                    if now - last_stats >= 0.5:
                        last_stats = now
                        self._emit_periodic()
                    consecutive_failures = 0
                except (ScpiError, ValueError) as exc:
                    consecutive_failures += 1
                    self.last_error = str(exc)
                    self.publish({"type": "error", "message": str(exc)})
                    if isinstance(exc, ScpiTimeout):
                        # A read timeout ends the run only when it cost us the
                        # acquisition.  On VXI-11 it does not: the RPC
                        # completed, the link is up and the instrument is still
                        # measuring, so one unanswered DATA:POIN? must not kill
                        # a long run.  It still counts towards
                        # ``consecutive_failures``, so a link that never
                        # answers again ends the run on the third try.
                        fatal = exc.link_reset
                    else:
                        fatal = isinstance(exc, ScpiConnectionError)
                    if fatal or consecutive_failures >= 3:
                        stop_reason = f"the run stopped: {exc}"
                        break
                    self._stream_stop.wait(0.2)
            left_loop = True
        finally:
            self._stream_stop.set()
            if not left_loop and stop_reason is None:
                stop_reason = (
                    "the run stopped: the streaming drain failed unexpectedly"
                )
            if stop_reason is not None:
                # ABOR matters here: without it the instrument keeps filling
                # its reading memory under TRIG:COUN INF with nobody draining.
                try:
                    self.abort_stream(stop_reason)
                except ScpiError as exc:
                    self.last_error = (
                        f"{stop_reason}; and the acquisition could not be "
                        f"aborted afterwards: {exc}"
                    )

    def _restart_stream_acquisition_locked(self) -> None:
        """Re-arm a run whose acquisition was stopped underneath it.

        ``ABOR`` first, so this is safe whether or not anything is still
        acquiring: an ``INIT`` on top of a live acquisition would only queue
        ``-213,"Init ignored"``.
        """
        self.ctrl.write("ABOR", priority=True)
        self.ctrl.write("TRIG:SOUR IMM")
        self.ctrl.write("TRIG:COUN INF")
        self.ctrl.write("SAMP:COUN 1")
        self.ctrl.write("INIT")
        message = (
            "the run had stopped measuring — no readings arrived for "
            f"{STREAM_RESTART_AFTER:g} s — so the acquisition was restarted"
        )
        self.last_error = message
        self.publish({"type": "error", "message": message})

    def _on_readings(self, values: List[float]) -> None:
        now = time.time()
        cutoff = time.monotonic() - 1.0
        with self._rate_lock:
            self._rate_events.append((time.monotonic(), len(values)))
            # Trim on append; rate_hz() is not guaranteed to be called.
            while self._rate_events and self._rate_events[0][0] < cutoff:
                self._rate_events.popleft()

        overloaded = [index for index, v in enumerate(values) if is_overload(v)]
        published = [None if is_overload(v) else float(v) for v in values]
        self._overload = bool(values) and is_overload(values[-1])

        if self._logging:
            overflowed_now = False
            with self._log_lock:
                room = MAX_LOG_POINTS - len(self._log)
                already = self._log_overflow
                if room <= 0:
                    self._log_overflow = True
                    overflowed_now = not already
                else:
                    take = published[:room]
                    if len(take) < len(published):
                        self._log_overflow = True
                        overflowed_now = not already
                    self._log.extend((now, v) for v in take)
            if overflowed_now:
                message = (
                    f"log buffer full at {MAX_LOG_POINTS} points; "
                    "further readings are not being logged"
                )
                self.last_error = message
                self.publish({"type": "error", "message": message})
                self.publish_state()

        message: Dict[str, Any] = {"type": "data", "t": now, "v": published}
        if overloaded:
            # Overloads are flagged, never published as 9.91E37: the UI shows
            # OVLD for these indices instead of plotting a decade-37 spike.
            message["ovld"] = overloaded
        self.publish(message)

    def _emit_periodic(self) -> None:
        with self._hold():
            stats_on = self.ctrl.query_bool("CALC:AVER:STAT?")
            stats = self._read_stats() if stats_on else None
            limit_on = self.ctrl.query_bool("CALC:LIM:STAT?")
            if limit_on:
                low = self.ctrl.query_float("CALC:LIM:LOW?")
                high = self.ctrl.query_float("CALC:LIM:UPP?")
                last = self._query_measurement("DATA:LAST?")
            else:
                low = high = last = 0.0
            queue = self._drain_errors_locked()
        if stats is not None:
            message: Dict[str, Any] = {"type": "stats"}
            message.update(stats)
            self.publish(message)
        if limit_on and not (
            is_overload(last) or is_overload(low) or is_overload(high)
        ):
            if last < low:
                status = "fail_low"
            elif last > high:
                status = "fail_high"
            else:
                status = "pass"
            if status != self._limit_status:
                self._limit_status = status
                self.publish({"type": "limit", "status": status, "value": last})
        elif limit_on:
            # An overloaded or not-yet-taken reading is not a limit failure.
            # Forget the last verdict so the next real reading republishes one.
            self._limit_status = None
        for entry in queue:
            self.publish({"type": "error", "message": f"instrument: {entry}"})

    def _drain_errors_locked(self) -> List[str]:
        """Read the error queue during a run; caller holds ``_gate``.

        ``-365 Reading memory overflow`` is the one that matters: it means the
        instrument dropped samples because this loop could not keep up, and it
        would otherwise be invisible.
        """
        found: List[str] = []
        for _ in range(5):
            raw = self.ctrl.query("SYST:ERR?")
            if raw.startswith("+0,") or raw.startswith("0,"):
                break
            found.append(raw)
            if "-365" in raw.split(",")[0]:
                self._memory_overflow = True
                self.last_error = (
                    "the instrument's reading memory overflowed; "
                    "samples were dropped. Reduce NPLC or stop other polling."
                )
        return found

    def rate_hz(self) -> float:
        cutoff = time.monotonic() - 1.0
        with self._rate_lock:
            while self._rate_events and self._rate_events[0][0] < cutoff:
                self._rate_events.popleft()
            total = sum(count for _, count in self._rate_events)
        return float(total)

    # ------------------------------------------------------------- math data

    def _read_stats(self) -> Dict[str, Any]:
        # With no accumulated readings every CALC:AVER: node answers 9.91E37.
        return {
            "avg": _no_overload(self.ctrl.query_float("CALC:AVER:AVER?")),
            "min": _no_overload(self.ctrl.query_float("CALC:AVER:MIN?")),
            "max": _no_overload(self.ctrl.query_float("CALC:AVER:MAX?")),
            "ptp": _no_overload(self.ctrl.query_float("CALC:AVER:PTP?")),
            "sdev": _no_overload(self.ctrl.query_float("CALC:AVER:SDEV?")),
            "count": self.ctrl.query_float("CALC:AVER:COUN?"),
        }

    def stats(self) -> Dict[str, Any]:
        with self._hold():
            stats = self._read_stats()
        self._note_ok()
        return stats

    def clear_stats(self) -> None:
        with self._hold():
            self.ctrl.write("CALC:AVER:CLE")
        self._note_ok()

    def histogram(self) -> Dict[str, Any]:
        with self._hold():
            values = self.ctrl.query_floats("CALC:TRAN:HIST:ALL?")
        if len(values) < 3:
            raise InstrumentError(
                f"CALC:TRAN:HIST:ALL? returned {len(values)} fields, expected >= 3"
            )
        self._note_ok()
        return {
            "lower": _no_overload(values[0]),
            "upper": _no_overload(values[1]),
            "count": int(values[2]),
            "bins": [int(v) for v in values[3:]],
        }

    def clear_histogram(self) -> None:
        with self._hold():
            self.ctrl.write("CALC:TRAN:HIST:CLE")
        self._note_ok()

    # -------------------------------------------------------------- logging

    @property
    def logging(self) -> bool:
        return self._logging

    def _read_reading_unit_locked(self, spec: FunctionSpec) -> str:
        """How the readings are expressed right now.  Caller holds ``_gate``.

        ``CALC:SCAL:FUNC DB`` or ``DBM`` makes the reading a logarithmic ratio,
        so it is not in the function's own unit.  The two scaling nodes are
        read rather than assumed: the exported CSV names the unit in its header
        and in every row, and a column of dB values labelled "V" is worse than
        no label at all.  (``CALC:SCAL:FUNC?`` can still answer ``NULL`` — this
        firmware's power-on value — and that is a subtraction in the function's
        own unit, so it correctly leaves this alone.)
        """
        unit = spec.unit
        if self.ctrl.query_bool("CALC:SCAL:STAT?"):
            scaling = self.ctrl.query("CALC:SCAL:FUNC?").strip().upper()
            if scaling.startswith("DBM"):
                unit = "dBm"
            elif scaling.startswith("DB"):
                unit = "dB"
        return unit

    def _note_reading_unit(self, func: str, unit: str) -> None:
        """Record how readings are expressed from this point on.

        Called from the state read (which already knows both) and from the two
        operations that can change them mid-recording.  While a recording is
        running the change opens a new segment in the log, so the rows already
        taken keep the unit they were actually measured in.
        """
        self._reading_func = func
        self._reading_unit = unit
        if not self._logging:
            return
        with self._log_lock:
            self._mark_log_segment_locked(func, unit)

    def _mark_log_segment_locked(self, func: str, unit: str) -> None:
        """Open a log segment at the current row if the labelling changed."""
        if self._log_units and self._log_units[-1][1:] == (func, unit):
            return
        boundary = len(self._log)
        if self._log_units and self._log_units[-1][0] == boundary:
            # Nothing was logged under the previous label; replace it rather
            # than leaving an empty segment behind.
            self._log_units[-1] = (boundary, func, unit)
        else:
            self._log_units.append((boundary, func, unit))

    def _unit_from_state(self, state: Dict[str, Any]) -> Tuple[str, str]:
        """``(func, unit)`` as the readings in *state* are expressed."""
        func = str(state.get("func") or "")
        unit = str(state.get("unit") or "")
        math_state = state.get("math") or {}
        if math_state.get("scale_on"):
            token = str(math_state.get("scale_func") or "").strip().upper()
            if token.startswith("DBM"):
                unit = "dBm"
            elif token.startswith("DB"):
                unit = "dB"
        return func, unit

    def start_log(self, note: str = "") -> None:
        with self._hold():
            spec = self.current_func()
            unit = self._read_reading_unit_locked(spec)
        self._reading_func = spec.key
        self._reading_unit = unit
        with self._log_lock:
            self._log_note = note or ""
            self._log_overflow = False
            self._mark_log_segment_locked(spec.key, unit)
        self._logging = True
        self._note_ok()

    def stop_log(self) -> None:
        self._logging = False

    def clear_log(self) -> None:
        with self._log_lock:
            self._log = []
            self._log_overflow = False
            self._log_units = []
            if self._logging:
                self._mark_log_segment_locked(
                    self._reading_func, self._reading_unit
                )

    def log_count(self) -> int:
        with self._log_lock:
            return len(self._log)

    @property
    def log_overflow(self) -> bool:
        with self._log_lock:
            return self._log_overflow

    def log_csv_chunks(self, rows_per_chunk: int = 2000) -> Iterator[str]:
        """Yield the log as CSV text in chunks.

        At MAX_LOG_POINTS the whole file is around 150 MB, so it is streamed
        rather than assembled in memory.  A reading that overloaded is written
        with an empty value and ``overload`` set to 1, never as 9.91E37.
        """
        with self._log_lock:
            rows = list(self._log)
            note = self._log_note
            segments = list(self._log_units) or [(0, "", "")]
            overflow = self._log_overflow
        if segments[0][0] > 0:
            # Rows logged before the first recorded label — there are none in
            # normal use, because start_log marks a segment before logging
            # begins — are described honestly rather than given the next
            # segment's unit.
            segments.insert(0, (0, "", ""))
        funcs = {segment[1] for segment in segments}
        units = {segment[2] for segment in segments}
        func = (
            next(iter(funcs))
            if len(funcs) == 1
            else "mixed — see the # label rows below"
        )
        unit = (
            next(iter(units))
            if len(units) == 1
            else "mixed — every row carries its own unit"
        )

        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")

        def flush() -> str:
            text = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return text

        writer.writerow(["# instrument", self.identity.get("idn", "")])
        writer.writerow(["# function", func, "unit", unit])
        writer.writerow(["# note", note])
        writer.writerow(["# points", len(rows)])
        if len(segments) > 1:
            # The function or the scaling changed while this buffer was
            # filling.  Every row already carries its own unit; these rows say
            # exactly where each label starts, so the file is readable without
            # comparing units column by column.
            for start, seg_func, seg_unit in segments:
                writer.writerow(
                    [
                        "# label",
                        "from row",
                        start,
                        "function",
                        seg_func or "unknown",
                        "unit",
                        seg_unit or "unknown",
                    ]
                )
        if overflow:
            writer.writerow(
                ["# truncated", f"buffer limit of {MAX_LOG_POINTS} points reached"]
            )
        writer.writerow(
            ["index", "epoch_s", "elapsed_s", "value", "unit", "overload"]
        )
        yield flush()

        t0 = rows[0][0] if rows else 0.0
        # Walk the segments in step with the rows, so each row is written with
        # the unit that was actually in force when it was measured.
        next_segment = 1
        row_unit = segments[0][2]
        for index, (timestamp, value) in enumerate(rows):
            while next_segment < len(segments) and index >= segments[next_segment][0]:
                row_unit = segments[next_segment][2]
                next_segment += 1
            writer.writerow(
                [
                    index,
                    f"{timestamp:.6f}",
                    f"{timestamp - t0:.6f}",
                    "" if value is None else repr(value),
                    row_unit,
                    1 if value is None else 0,
                ]
            )
            if (index + 1) % rows_per_chunk == 0:
                yield flush()
        tail = flush()
        if tail:
            yield tail

    # --------------------------------------------------------------- screen

    def capture_screen(self) -> Tuple[bytes, float]:
        """Grab the instrument screen once, because a user asked for it.

        Rule 4: there is no capture thread and no timer.  Continuous mirroring
        was withdrawn — it re-displayed state this application already holds
        and renders natively, and at 277 KB a frame it was the heaviest load on
        the instrument's LAN stack.  This is the deliberate single grab that
        remains, for documentation and reporting.

        It runs on the one ``ctrl`` link, pausing the acquisition around itself
        rather than opening a second socket for the ~0.16 s it blocks.
        """
        with self.pause_acquisition():
            if not self._screen_format_set:
                # PNG is a supported format but takes 2.56 s against BMP's
                # 0.16 s (SPEC.md section 1); BMP is decoded locally instead.
                self.ctrl.write("HCOP:SDUM:DATA:FORM BMP")
                self._screen_format_set = True
            try:
                raw = self.ctrl.query_block("HCOP:SDUM:DATA?", timeout=SCREEN_TIMEOUT)
            except ScpiError:
                # A failed grab may mean the format latch is no longer true.
                self._screen_format_set = False
                raise
        try:
            png = bmp_to_png(raw)
        except ScreenDecodeError:
            self._screen_format_set = False
            raise
        stamp = time.time()
        with self._frame_lock:
            self._frame = png
            self._frame_time = stamp
        self._note_ok()
        return png, stamp

    def last_capture(self) -> Tuple[Optional[bytes], float]:
        """The most recent user-requested capture, or ``(None, 0.0)``.

        Never grabs: a caller asking what it already has must not cost the
        instrument a 277 KB transfer.
        """
        with self._frame_lock:
            return self._frame, self._frame_time

    # --------------------------------------------------------------- system

    def beep(self) -> None:
        with self._hold():
            self.ctrl.write("SYST:BEEP")
        self._note_ok()

    def selftest(self) -> Dict[str, Any]:
        with self._hold(may_change_trigger=True):
            was_streaming = self._streaming
            if was_streaming:
                self.ctrl.write("ABOR", priority=True)
            try:
                result = self.ctrl.query("*TST?", timeout=SELFTEST_TIMEOUT)
                errors = self.errors()
            finally:
                self._resume_after_change(was_streaming)
        try:
            passed = int(float(result)) == 0
        except ValueError:
            passed = False
        self._note_ok()
        return {"result": result, "passed": passed, "errors": errors}

    def lock(self, acquire: bool) -> Dict[str, Any]:
        with self._hold():
            if acquire:
                granted = self.ctrl.query("SYST:LOCK:REQ?").strip()
                owner = self.ctrl.query("SYST:LOCK:OWN?")
                result = {
                    "acquired": granted in ("1", "+1"),
                    "granted": granted,
                    "owner": owner,
                }
            else:
                self.ctrl.write("SYST:LOCK:REL")
                owner = self.ctrl.query("SYST:LOCK:OWN?")
                result = {"acquired": False, "granted": "0", "owner": owner}
        self._note_ok()
        return result

    def reset(self) -> None:
        was_streaming = self._streaming
        if was_streaming:
            self.stop_stream()
        with self._hold(may_change_trigger=True):
            self.ctrl.write("ABOR", priority=True)
            self.ctrl.write("*RST")
            self.ctrl.write("*CLS")
            # *RST puts the trigger system at its power-on setup, so anything
            # saved from before it is stale.  Keeping it would make shutdown
            # write the pre-reset values back over the setup the user just
            # asked for.  The keepalive captures the new one on its next start.
            self._trig_saved = None
            self._limit_status = None
            self._overload = False
            self._memory_overflow = False
            # *RST returns the hardcopy format to its power-on value, so the
            # one-shot "format already set" latch is no longer true.
            self._screen_format_set = False
        self.note_setup_changed()
        self._note_ok()

    def passthrough(self, command: str) -> Dict[str, Any]:
        """Send a user-typed command. Unsupported ones are allowed but fenced.

        The guard is bypassed on purpose: this is the expert console.  A stalled
        query is caught after 3 s, the link is rebuilt by :class:`ScpiLink`, and
        the failure is reported rather than swallowed.
        """
        command = command.strip()
        if not command:
            raise InstrumentError("no command supplied")
        if "\n" in command or "\r" in command:
            raise InstrumentError("send one command per request")
        is_query = command_is_query(command)
        started = time.perf_counter()
        stop_run = False
        # A user-typed command can move the trigger system in any direction, so
        # the keepalive re-reads the source before driving anything again.
        with self._hold(may_change_trigger=True):
            try:
                if is_query:
                    response = self.ctrl.query(
                        command, timeout=PASSTHROUGH_TIMEOUT, guard=False
                    )
                    error = None
                else:
                    self.ctrl.write(command, guard=False)
                    response = ""
                    # A rejected command is silent on the wire; the only way to
                    # tell it apart from a successful one is the error queue.
                    error = self._passthrough_errors()
            except ScpiTimeout as exc:
                response = ""
                error = str(exc)
                if self._streaming and exc.link_reset:
                    # The rebuilt socket lost the acquisition.  Stop the run
                    # properly — ABOR, restore the user's trigger setup, tell
                    # the clients — instead of leaving a zombie "Running".
                    #
                    # Only when the link really was reset.  On VXI-11 the
                    # timeout arrived as a completed RPC: the link is still up
                    # and in step, the instrument never saw a device clear and
                    # the acquisition is untouched, so stopping the run would
                    # be gratuitous and the sentence below would be false.
                    stop_run = True
                    error += " — the run was stopped because the link was reset"
            except ScpiConnectionError as exc:
                response = ""
                error = str(exc)
                if self._streaming:
                    # A stalled write is the other way a console command costs
                    # us the acquisition: the session is torn down
                    # mid-exchange, and on VXI-11 destroying the sole link
                    # makes the instrument device-clear itself.  Without this
                    # arm ``_streaming`` stayed True and the UI went on saying
                    # "Running" until STREAM_RESTART_AFTER noticed, ten seconds
                    # later.
                    stop_run = True
                    error += " — the run was stopped because the link was reset"
            except ScpiError as exc:
                response = ""
                error = str(exc)
        # A user-typed command can move the trigger system in any direction,
        # so the heartbeat re-reads rather than trusting its cached verdict.
        self.note_setup_changed()
        if stop_run:
            self.abort_stream("the SCPI console reset the link")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if error is None:
            self._note_ok()
        else:
            self.last_error = error
        return {
            "response": response,
            "is_query": is_query,
            "error": error,
            "elapsed_ms": round(elapsed_ms, 3),
        }

    def _passthrough_errors(self) -> Optional[str]:
        """Drain SYST:ERR? after a user-typed write; caller holds ``_gate``."""
        try:
            found = self.errors(limit=5)
        except ScpiError as exc:
            return f"the command was sent but the error queue could not be read: {exc}"
        if not found:
            return None
        return "; ".join(found)


# ---------------------------------------------------------------- safety net


def emergency_release(reason: str) -> List[str]:
    """Hand every live instrument back, best effort, from a dying process.

    Defence in depth, not the primary protection.  The primary protection is
    the keepalive's finite trigger count: no handler installed here survives a
    hard kill, a power cut or a segfault, and trigger state is global to the
    instrument rather than owned by our session, so a Python-side hook can
    never be the thing that keeps reading memory empty.

    What this *can* do is cover the ordinary crashes — an unhandled exception
    in a Qt slot, a Ctrl-C, a SIGTERM — where the process still gets to run
    code but ``closeEvent`` never will.  It sends only writes, takes the gate
    with a short timeout so it cannot hang behind a worker thread, and reports
    what it did on stderr because by this point there is no UI left to tell.
    """
    done: List[str] = []
    for dmm in list(_LIVE_INSTRUMENTS):
        if not dmm.ctrl.connected:
            continue
        got_gate = dmm._gate.acquire(timeout=1.0)
        try:
            dmm._idle_initiated = False
            dmm._streaming = False
            dmm._stream_stop.set()
            dmm._beat_stop.set()
            try:
                dmm._restore_trigger_locked()
                dmm.ctrl.write("SYST:LOC", priority=True)
                done.append(f"{dmm.host}: trigger restored and returned to local")
            except ScpiError as exc:
                done.append(f"{dmm.host}: could not hand back cleanly: {exc}")
        finally:
            if got_gate:
                dmm._gate.release()
        dmm.ctrl.close()
    if done:
        sys.stderr.write(
            f"instrument safety net ({reason}): " + "; ".join(done) + "\n"
        )
    return done


def install_safety_net() -> None:
    """Install the crash handlers once per process (finding 9).

    ``atexit`` covers a normal interpreter exit that skipped the window close,
    the two excepthooks cover an unhandled exception on the GUI thread or in a
    worker — PySide6 tears the process down on the former, so ``closeEvent``
    never runs — and SIGINT/SIGTERM/SIGBREAK cover a console interrupt or a
    shutdown request.  All of them are additive: the previous handler still
    runs, so nothing that was already installed is silently replaced.
    """
    global _SAFETY_NET_INSTALLED
    with _SAFETY_NET_LOCK:
        if _SAFETY_NET_INSTALLED:
            return
        _SAFETY_NET_INSTALLED = True

    atexit.register(emergency_release, "interpreter exit")

    previous_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        emergency_release(f"unhandled {exc_type.__name__}")
        previous_hook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    previous_thread_hook = threading.excepthook

    def _thread_excepthook(args):
        name = getattr(args.exc_type, "__name__", "exception")
        emergency_release(f"unhandled {name} in thread {args.thread and args.thread.name}")
        previous_thread_hook(args)

    threading.excepthook = _thread_excepthook

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous_signal = signal.getsignal(number)
        except (OSError, ValueError):
            continue

        def _handler(signum, frame, _previous=previous_signal, _name=name):
            emergency_release(_name)
            if callable(_previous):
                _previous(signum, frame)
            elif _previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        try:
            signal.signal(number, _handler)
        except (OSError, ValueError):
            # Not the main thread, or the platform will not take a handler for
            # this signal; the other handlers still stand.
            continue
