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

"""Drive the console against the live instrument and save screenshots.

Not part of the application.  It builds the real window, runs a scripted
sequence of user actions on the GUI thread, and grabs the window after each
step so the result can be looked at rather than assumed.

    python tools/shoot.py [--instrument 192.0.2.50] [--out screenshots]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.main import build_application  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", default="192.0.2.50")
    parser.add_argument("--out", default="screenshots")
    parser.add_argument("--script", default="tour")
    arguments = parser.parse_args()

    out = os.path.abspath(arguments.out)
    os.makedirs(out, exist_ok=True)

    application, window = build_application(["--instrument", arguments.instrument])
    window.show()

    steps = SCRIPTS[arguments.script]
    state = {"index": 0, "started": time.time()}

    def shoot(name: str) -> None:
        path = os.path.join(out, f"{name}.png")
        window.grab().save(path, "PNG")
        print(f"  saved {path}", flush=True)

    def run_next() -> None:
        if state["index"] >= len(steps):
            print("done; closing", flush=True)
            window.close()
            QTimer.singleShot(600, application.quit)
            return
        delay, label, action = steps[state["index"]]
        state["index"] += 1
        print(f"[{time.time() - state['started']:6.1f}s] {label}", flush=True)
        try:
            action(window, shoot)
        except Exception as exc:  # surfaced, never swallowed
            print(f"  STEP FAILED: {type(exc).__name__}: {exc}", flush=True)
        QTimer.singleShot(delay, run_next)

    QTimer.singleShot(1500, run_next)
    code = int(application.exec())

    # Shutdown audit: after the window closed, nothing of ours may be left.
    import threading

    worker = window.bridge.worker
    survivors = [
        t.name
        for t in threading.enumerate()
        if t is not threading.main_thread() and t.is_alive()
    ]
    print("--- shutdown audit", flush=True)
    print(f"  exec returned {code}", flush=True)
    thread = getattr(window.bridge, "thread", None)
    print(
        "  worker QThread running: "
        f"{thread.isRunning() if thread is not None else 'thread released'}",
        flush=True,
    )
    if worker is None:
        print("  worker released", flush=True)
    else:
        print(f"  streaming: {worker.dmm.streaming}", flush=True)
        print(f"  ctrl link open: {worker.dmm.ctrl.connected}", flush=True)
    print(f"  surviving non-main threads: {survivors or 'none'}", flush=True)
    return code


def _function(key: str):
    def go(window, shoot) -> None:
        window.bridge.setFunction.emit(key)

    return go


def _config(changes: dict):
    def go(window, shoot) -> None:
        window.bridge.setConfig.emit(changes)

    return go


def _math(changes: dict):
    def go(window, shoot) -> None:
        window.bridge.setMath.emit(changes)

    return go


def _trigger(changes: dict):
    def go(window, shoot) -> None:
        window.bridge.setTrigger.emit(changes)

    return go


def _display(changes: dict):
    def go(window, shoot) -> None:
        window.bridge.setDisplay.emit(changes)

    return go


def _stream(run: bool):
    def go(window, shoot) -> None:
        window.bridge.setStream.emit(run)

    return go


def _screen(run: bool):
    """One on-demand grab (rule 4 withdrew continuous mirroring entirely).

    ``run`` is kept so the existing scripts read unchanged; a False step is now
    a no-op because there is no mirror to turn off.
    """

    def go(window, shoot) -> None:
        if run:
            window.bridge.captureScreen.emit()

    return go


def _shot(name: str):
    def go(window, shoot) -> None:
        shoot(name)

    return go


def _tab(index: int):
    def go(window, shoot) -> None:
        window.tabs.setCurrentIndex(index)

    return go


def _scpi(command: str):
    def go(window, shoot) -> None:
        window.bridge.passthrough.emit(command)

    return go


def _log(run: bool):
    def go(window, shoot) -> None:
        window.bridge.setLog.emit(run, "overload check")

    return go


def _export_csv(window, shoot) -> None:
    path = os.path.join(os.path.abspath("screenshots"), "overload-log.csv")
    window.bridge.exportLog.emit(path)
    print(f"  exporting to {path}", flush=True)


def _report(window, shoot) -> None:
    state = window.state or {}
    print(
        "  state: func=%s range=%s auto=%s res=%s nplc=%s rate=%.1f streaming=%s"
        % (
            state.get("func"),
            state.get("range"),
            state.get("range_auto"),
            state.get("resolution"),
            state.get("nplc"),
            state.get("rate_hz") or 0.0,
            state.get("streaming"),
        ),
        flush=True,
    )


TOUR = [
    (1200, "connected", _report),
    (400, "shot: idle", _shot("01-idle")),
    (2500, "start screen mirror", _screen(True)),
    (600, "shot: mirror", _shot("02-mirror")),
    (2500, "start stream", _stream(True)),
    (1500, "report", _report),
    (400, "shot: running", _shot("03-running")),
    (2000, "stats on", _math({"stats_on": True})),
    (2500, "limits on", _math({"limit_on": True, "limit_low": -1.0, "limit_high": 1.0})),
    (600, "shot: stats+limits", _shot("04-stats-limits")),
    (2000, "histogram on", _math({"hist_on": True, "hist_points": 100})),
    (1500, "tab: histogram", _tab(1)),
    (2500, "shot: histogram", _shot("05-histogram")),
    (500, "tab: log", _tab(2)),
    (1500, "shot: log empty", _shot("06a-log-empty")),
    (500, "record the log", _log(True)),
    (4000, "shot: log", _shot("06-log")),
    (500, "stop log", _log(False)),
    (500, "tab: scpi", _tab(3)),
    (500, "scpi: *IDN?", _scpi("*IDN?")),
    (1500, "scpi: bogus", _scpi("FOO:BAR?")),
    (4000, "shot: scpi", _shot("07-scpi")),
    (500, "tab: system", _tab(4)),
    (2000, "shot: system", _shot("08-system")),
    (500, "tab: chart", _tab(0)),
    (500, "stop stream", _stream(False)),
    (2500, "report", _report),
    (500, "shot: stopped", _shot("09-stopped")),
]

FUNCTIONS = []
for key, name in (
    ("VOLT:DC", "dcv"),
    ("VOLT:AC", "acv"),
    ("CURR:DC", "dci"),
    ("CURR:AC", "aci"),
    ("RES", "res"),
    ("FRES", "fres"),
    ("FREQ", "freq"),
    ("PER", "per"),
    ("CAP", "cap"),
    ("CONT", "cont"),
    ("DIOD", "diod"),
    ("TEMP", "temp"),
):
    FUNCTIONS.append((2500, f"function {key}", _function(key)))
    FUNCTIONS.append((900, f"report {key}", _report))
    FUNCTIONS.append((300, f"shot {name}", _shot(f"fn-{name}")))

CONFIG = [
    (1200, "connected", _report),
    (2000, "DCV", _function("VOLT:DC")),
    (2000, "range 0.1", _config({"range": 0.1})),
    (900, "report", _report),
    (2000, "range 1000", _config({"range": 1000.0})),
    (900, "report", _report),
    (400, "shot 1000V", _shot("cfg-1000v")),
    (2000, "auto range", _config({"range_auto": True})),
    (900, "report", _report),
    (2000, "nplc 0.02", _config({"nplc": 0.02})),
    (900, "report", _report),
    (2000, "nplc 10", _config({"nplc": 10.0})),
    (900, "report", _report),
    (2000, "azero off", _config({"azero": "OFF"})),
    (900, "report", _report),
    (2000, "azero on", _config({"azero": "ON"})),
    (2000, "imped HIZ", _config({"impedance": "HIZ"})),
    (900, "report", _report),
    (2000, "imped 10M", _config({"impedance": "10M"})),
    (2000, "ACV", _function("VOLT:AC")),
    (2000, "band 3", _config({"band": 3})),
    (900, "report", _report),
    (2000, "band 200", _config({"band": 200})),
    (2000, "FREQ", _function("FREQ")),
    (2000, "aperture 1", _config({"aperture": 1.0})),
    (900, "report", _report),
    (400, "shot freq", _shot("cfg-freq")),
    (2000, "TEMP", _function("TEMP")),
    (2000, "probe THER", _config({"temp_type": "THER"})),
    (2000, "unit F", _config({"temp_unit": "F"})),
    (900, "report", _report),
    (400, "shot temp", _shot("cfg-temp")),
    (2000, "unit C", _config({"temp_unit": "C"})),
    (2000, "probe FRTD", _config({"temp_type": "FRTD"})),
    (2000, "rtd 100", _config({"rtd_res": 100.0})),
    (2000, "DCV", _function("VOLT:DC")),
    (2000, "trigger BUS", _trigger({"source": "BUS"})),
    (900, "report", _report),
    (2000, "trigger IMM", _trigger({"source": "IMM"})),
    (2000, "slope NEG", _trigger({"slope": "NEG"})),
    (2000, "slope POS", _trigger({"slope": "POS"})),
    (2000, "delay 0.01", _trigger({"delay": 0.01})),
    (2000, "delay auto", _trigger({"delay_auto": True})),
    (2000, "samples 4", _trigger({"samples": 4})),
    (2000, "samples 1", _trigger({"samples": 1})),
    (2000, "null on", _math({"null_on": True})),
    (2000, "null auto", _math({"null_auto": True})),
    (2000, "null off", _math({"null_on": False})),
    (2000, "scale DB", _math({"scale_on": True, "scale_func": "DB", "db_ref": 1.0})),
    (2000, "scale DBM", _math({"scale_func": "DBM", "dbm_ref": 50.0})),
    (2000, "scale off", _math({"scale_on": False})),
    (900, "report", _report),
    (400, "shot end", _shot("cfg-end")),
]

def _dump_chart(window, shoot) -> None:
    chart = window.chart_panel.chart
    n = chart._n
    values = chart._vs[:n]
    import numpy as np

    gaps = int(np.isnan(values).sum())
    print(
        f"  chart: {n} points, {gaps} overloaded (NaN, drawn as gaps), "
        f"finite min={np.nanmin(values) if n - gaps else float('nan'):.6g}",
        flush=True,
    )


OVERLOAD = [
    (1500, "connected", _report),
    (2500, "2-wire resistance", _function("RES")),
    (2500, "fixed 100 ohm range", _config({"range": 100.0, "range_auto": False})),
    (1500, "nplc 0.2 for a faster trace", _config({"nplc": 0.2})),
    (2500, "start stream", _stream(True)),
    (5000, "report", _report),
    (500, "shot overload readout", _shot("ovld-1-readout")),
    (500, "chart contents", _dump_chart),
    (3000, "shot overload chart", _shot("ovld-2-chart")),
    (500, "record the log", _log(True)),
    (4000, "stop log", _log(False)),
    (500, "chart contents", _dump_chart),
    (500, "export csv", _export_csv),
    (2500, "stop", _stream(False)),
    (2000, "auto range back", _config({"range_auto": True})),
    (1500, "shot stopped", _shot("ovld-3-stopped")),
]

def _type_scpi(command: str):
    """Drive the console the way a user does, so the echo and guard run."""

    def go(window, shoot) -> None:
        window.tabs.setCurrentIndex(3)
        window.console_panel.input.setText(command)
        window.console_panel._submit()

    return go


CONSOLE = [
    (1500, "connected", _report),
    (2500, "start stream", _stream(True)),
    (3000, "report while running", _report),
    (500, "unsupported WRITE: FOO:BAR", _type_scpi("FOO:BAR")),
    (3000, "report", _report),
    (500, "shot after bad write", _shot("con-1-bad-write")),
    (500, "supported query", _type_scpi("SYST:UPT?")),
    (2500, "shot after good query", _shot("con-2-good-query")),
    (500, "unsupported QUERY: FOO:BAR?", _type_scpi("FOO:BAR?")),
    (6000, "report after stalled query", _report),
    (500, "shot after stalled query", _shot("con-3-stalled-query")),
    (500, "link recovered? query again", _type_scpi("*IDN?")),
    (2500, "shot after recovery", _shot("con-4-recovered")),
    (500, "never-send list: first press", _type_scpi("TRIG:LEV?")),
    (1500, "shot: guard warning", _shot("con-5-guard")),
    (500, "report", _report),
    (500, "stop", _stream(False)),
]


# ------------------------------------------------------------- endurance run

def _rss_bytes() -> int:
    """Resident set size of this process, without a third-party dependency."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = COUNTERS()
        counters.cb = ctypes.sizeof(COUNTERS)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        return int(counters.WorkingSetSize)
    with open(f"/proc/{os.getpid()}/statm") as handle:
        return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


class PaintWatch:
    """Wrap the window's 30 Hz tick to measure GUI-thread responsiveness."""

    def __init__(self, window) -> None:
        self.window = window
        self.worst = 0.0
        self.count = 0
        self._last = time.monotonic()
        self._inner = window._tick
        window._tick = self.tick

    def tick(self) -> None:
        now = time.monotonic()
        gap = now - self._last
        self._last = now
        if self.count:
            self.worst = max(self.worst, gap)
        self.count += 1
        self._inner()

    def reset(self) -> None:
        self.worst = 0.0


WATCH = {}


def _watch_start(window, shoot) -> None:
    WATCH["paint"] = PaintWatch(window)
    WATCH["rss0"] = _rss_bytes()
    WATCH["t0"] = time.monotonic()
    print(f"  baseline RSS {WATCH['rss0'] / 1e6:.1f} MB", flush=True)


def _watch_report(window, shoot) -> None:
    watch = WATCH.get("paint")
    rss = _rss_bytes()
    state = window.state or {}
    chart = window.chart_panel.chart
    print(
        "  t=%5.0fs  rate=%6.1f/s  chart=%7d pts  log=%8s  RSS=%7.1f MB "
        "(%+6.1f MB)  worst paint gap=%5.0f ms"
        % (
            time.monotonic() - WATCH["t0"],
            state.get("rate_hz") or 0.0,
            chart.count(),
            f"{state.get('log_count') or 0:,}",
            rss / 1e6,
            (rss - WATCH["rss0"]) / 1e6,
            (watch.worst if watch else 0.0) * 1000.0,
        ),
        flush=True,
    )
    if watch:
        watch.reset()


ENDURANCE = [
    (1500, "connected", _report),
    (2000, "DCV", _function("VOLT:DC")),
    (2000, "NPLC 0.02 — the fastest this meter streams", _config({"nplc": 0.02})),
    (2000, "auto range off, 10 V fixed", _config({"range": 10.0, "range_auto": False})),
    (2000, "screen mirror on", _screen(True)),
    (2000, "stats + histogram on", _math({"stats_on": True, "hist_on": True})),
    (1000, "start watch", _watch_start),
    (1000, "start stream", _stream(True)),
    (500, "record the log", _log(True)),
]
for _i in range(16):
    ENDURANCE.append((15000, "", _watch_report))
ENDURANCE += [
    (500, "shot endurance", _shot("endurance")),
    (500, "stop log", _log(False)),
    (2000, "stop stream", _stream(False)),
    (2000, "final", _watch_report),
    (500, "screen mirror off", _screen(False)),
]


# ------------------------------------------------------- axis stability watch

AXIS = {}


def _axis_watch(window, shoot) -> None:
    from PySide6.QtCore import QTimer as _QTimer

    chart = window.chart_panel.chart
    AXIS["samples"] = []
    AXIS["changes"] = 0
    AXIS["last"] = None
    AXIS["min"] = None
    AXIS["max"] = None

    def sample() -> None:
        current = (chart.y_min, chart.y_max)
        if AXIS["last"] is not None and current != AXIS["last"]:
            AXIS["changes"] += 1
        AXIS["last"] = current
        AXIS["samples"].append(current)

    timer = _QTimer(window)
    timer.timeout.connect(sample)
    timer.start(100)
    AXIS["timer"] = timer
    print("  watching the value axis at 10 Hz", flush=True)


def _axis_report(window, shoot) -> None:
    samples = AXIS.get("samples") or []
    timer = AXIS.get("timer")
    if timer is not None:
        timer.stop()
    if not samples:
        print("  no axis samples", flush=True)
        return
    lows = [a for a, _ in samples]
    highs = [b for _, b in samples]
    print(
        "  axis over %.1f s: %d changes in %d samples; y_min %.6g..%.6g  "
        "y_max %.6g..%.6g"
        % (
            len(samples) * 0.1,
            AXIS["changes"],
            len(samples),
            min(lows),
            max(lows),
            min(highs),
            max(highs),
        ),
        flush=True,
    )
    chart = window.chart_panel.chart
    import numpy as np

    n = chart._n
    if n:
        v = chart._vs[:n]
        print(
            "  data in buffer: %d pts, min=%.6g max=%.6g ; axis now [%.6g, %.6g] "
            "(clipping: %s)"
            % (
                n,
                float(np.nanmin(v)),
                float(np.nanmax(v)),
                chart.y_min,
                chart.y_max,
                "YES" if float(np.nanmin(v)) < chart.y_min
                or float(np.nanmax(v)) > chart.y_max else "no",
            ),
            flush=True,
        )
    AXIS["samples"] = []
    AXIS["changes"] = 0
    AXIS["last"] = None


SCALE = [
    (1500, "connected", _report),
    (2000, "DCV", _function("VOLT:DC")),
    (2000, "10 V fixed range, leads open", _config({"range": 10.0, "range_auto": False})),
    (2000, "NPLC 0.02 for a noisy trace", _config({"nplc": 0.02})),
    (2000, "null off to start clean", _math({"null_on": False, "null_value": 0.0})),
    (2000, "start stream", _stream(True)),
    (3000, "watch the axis", _axis_watch),
    (35000, "steady state, 35 s", _report),
    (500, "shot steady", _shot("scale-1-steady")),
    (500, "axis report", _axis_report),
    (500, "watch again", _axis_watch),
    (2000, "STEP: null 0.02 V shifts every reading", _math({"null_value": 0.02, "null_on": True})),
    (1500, "shot just after the step", _shot("scale-2-step")),
    (3000, "axis report after step", _axis_report),
    (500, "watch again", _axis_watch),
    (10000, "settle after the step", _report),
    (500, "shot settled", _shot("scale-3-settled")),
    (500, "axis report", _axis_report),
    (2000, "null off", _math({"null_on": False})),
    (2000, "stop", _stream(False)),
]


def _resize(w: int, h: int):
    def go(window, shoot) -> None:
        window.resize(w, h)

    return go


def _crop_readout(name: str):
    """Save just the readout, scaled up, so the glyph spacing can be judged."""

    def go(window, shoot) -> None:
        pixmap = window.readout.grab()
        big = pixmap.scaled(
            pixmap.width() * 2,
            pixmap.height() * 2,
            aspectMode=Qt.KeepAspectRatio,
            mode=Qt.FastTransformation,
        )
        path = os.path.join(os.path.abspath("screenshots"), f"{name}.png")
        big.save(path, "PNG")
        print(f"  saved {path} ({big.width()}x{big.height()})", flush=True)

    return go


READOUT = [
    (1500, "connected", _report),
    (2500, "DCV", _function("VOLT:DC")),
    (2500, "10 V fixed", _config({"range": 10.0, "range_auto": False, "nplc": 1.0})),
    (2500, "single reading", lambda w, s: w.bridge.single.emit()),
    (1500, "crop DCV wide", _crop_readout("readout-1-dcv-10v-wide")),
    (500, "narrow window", _resize(1100, 760)),
    (1500, "crop DCV narrow", _crop_readout("readout-2-dcv-10v-narrow")),
    (500, "wide again", _resize(1900, 1000)),
    (1500, "crop DCV wider", _crop_readout("readout-3-dcv-10v-wider")),
    (500, "back to default", _resize(1500, 940)),
    (2500, "1000 V range", _config({"range": 1000.0})),
    (2000, "single reading", lambda w, s: w.bridge.single.emit()),
    (1500, "crop DCV 1000 V", _crop_readout("readout-4-dcv-1000v")),
    (2500, "2-wire resistance", _function("RES")),
    (2500, "1 kohm fixed", _config({"range": 1000.0, "range_auto": False})),
    (2500, "single reading", lambda w, s: w.bridge.single.emit()),
    (1500, "crop 2W", _crop_readout("readout-5-res-1k")),
    (2500, "frequency (no reported resolution)", _function("FREQ")),
    (2500, "single reading", lambda w, s: w.bridge.single.emit()),
    (1500, "crop FREQ", _crop_readout("readout-6-freq")),
    (2500, "capacitance", _function("CAP")),
    (2500, "single reading", lambda w, s: w.bridge.single.emit()),
    (1500, "crop CAP", _crop_readout("readout-7-cap")),
    (2500, "DCV back, auto", _function("VOLT:DC")),
    (2000, "blank state", lambda w, s: w.readout.clear_value()),
    (1000, "crop blank", _crop_readout("readout-8-blank")),
]


SHUTDOWN = [
    (1500, "connected", _report),
    (2000, "screen mirror on", _screen(True)),
    (2000, "start stream", _stream(True)),
    (1000, "record the log", _log(True)),
    (4000, "running", _report),
    (500, "shot before close", _shot("shutdown-running")),
]


# ---------------------------------------------------------- round-trip check

CHECKS = {"pass": 0, "fail": 0}


def _expect(getter, want, label):
    """Assert the state the instrument reported back matches what was asked."""

    def go(window, shoot) -> None:
        state = window.state or {}
        got = getter(state)
        if isinstance(want, float) and isinstance(got, (int, float)):
            ok = abs(float(got) - want) <= max(abs(want) * 1e-6, 1e-12)
        else:
            ok = got == want
        CHECKS["pass" if ok else "fail"] += 1
        verdict = "PASS" if ok else "FAIL"
        print(
            "  %s  %s: asked %r, instrument reports %r" % (verdict, label, want, got),
            flush=True,
        )

    return go


def _checks_report(window, shoot) -> None:
    print(
        "  === round-trip checks: %d passed, %d failed"
        % (CHECKS["pass"], CHECKS["fail"]),
        flush=True,
    )


def _g(*path):
    def get(state):
        node = state
        for key in path:
            node = (node or {}).get(key)
        return node

    return get


_STEPS = [
    ({"range": 0.1, "range_auto": False}, _g("range"), 0.1, "VOLT:DC:RANG 0.1", "cfg"),
    ({"range": 1000.0}, _g("range"), 1000.0, "VOLT:DC:RANG 1000", "cfg"),
    ({"range_auto": True}, _g("range_auto"), True, "VOLT:DC:RANG:AUTO ON", "cfg"),
    ({"nplc": 0.02}, _g("nplc"), 0.02, "VOLT:DC:NPLC 0.02", "cfg"),
    ({"nplc": 100.0}, _g("nplc"), 100.0, "VOLT:DC:NPLC 100", "cfg"),
    ({"nplc": 10.0}, _g("nplc"), 10.0, "VOLT:DC:NPLC 10", "cfg"),
    ({"azero": "OFF"}, _g("azero"), "OFF", "VOLT:DC:ZERO:AUTO OFF", "cfg"),
    ({"azero": "ON"}, _g("azero"), "ON", "VOLT:DC:ZERO:AUTO ON", "cfg"),
    ({"impedance": "HIZ"}, _g("impedance"), "HIZ", "VOLT:DC:IMP:AUTO ON", "cfg"),
    ({"impedance": "10M"}, _g("impedance"), "10M", "VOLT:DC:IMP:AUTO OFF", "cfg"),
    ({"source": "BUS"}, _g("trigger", "source"), "BUS", "TRIG:SOUR BUS", "trig"),
    ({"source": "EXT"}, _g("trigger", "source"), "EXT", "TRIG:SOUR EXT", "trig"),
    ({"source": "IMM"}, _g("trigger", "source"), "IMM", "TRIG:SOUR IMM", "trig"),
    ({"slope": "NEG"}, _g("trigger", "slope"), "NEG", "TRIG:SLOP NEG", "trig"),
    ({"slope": "POS"}, _g("trigger", "slope"), "POS", "TRIG:SLOP POS", "trig"),
    (
        {"delay_auto": False, "delay": 0.25},
        _g("trigger", "delay"),
        0.25,
        "TRIG:DEL 0.25",
        "trig",
    ),
    ({"delay_auto": True}, _g("trigger", "delay_auto"), True, "TRIG:DEL:AUTO ON", "trig"),
    ({"count": 7}, _g("trigger", "count"), 7, "TRIG:COUN 7", "trig"),
    ({"count": "INF"}, _g("trigger", "count"), "INF", "TRIG:COUN INF", "trig"),
    ({"count": 1}, _g("trigger", "count"), 1, "TRIG:COUN 1", "trig"),
    ({"samples": 5}, _g("trigger", "samples"), 5, "SAMP:COUN 5", "trig"),
    ({"samples": 1}, _g("trigger", "samples"), 1, "SAMP:COUN 1", "trig"),
    (
        {"null_value": 0.125, "null_on": True},
        _g("math", "null_value"),
        0.125,
        "VOLT:DC:NULL:VAL 0.125",
        "math",
    ),
    ({"null_on": True}, _g("math", "null_on"), True, "VOLT:DC:NULL:STAT ON", "math"),
    ({"null_auto": True}, _g("math", "null_auto"), True, "VOLT:DC:NULL:VAL:AUTO ON", "math"),
    ({"null_on": False}, _g("math", "null_on"), False, "VOLT:DC:NULL:STAT OFF", "math"),
    (
        {"scale_func": "DB", "db_ref": 3.0, "scale_on": True},
        _g("math", "scale_func"),
        "DB",
        "CALC:SCAL:FUNC DB",
        "math",
    ),
    ({"db_ref": 3.0}, _g("math", "db_ref"), 3.0, "CALC:SCAL:DB:REF 3", "math"),
    (
        {"scale_func": "DBM", "dbm_ref": 75.0},
        _g("math", "dbm_ref"),
        75.0,
        "CALC:SCAL:DBM:REF 75",
        "math",
    ),
    ({"scale_on": False}, _g("math", "scale_on"), False, "CALC:SCAL:STAT OFF", "math"),
    ({"stats_on": True}, _g("math", "stats_on"), True, "CALC:AVER:STAT ON", "math"),
    ({"stats_on": False}, _g("math", "stats_on"), False, "CALC:AVER:STAT OFF", "math"),
    (
        {"limit_low": -2.5, "limit_high": 2.5, "limit_on": True},
        _g("math", "limit_low"),
        -2.5,
        "CALC:LIM:LOW -2.5",
        "math",
    ),
    ({"limit_high": 2.5}, _g("math", "limit_high"), 2.5, "CALC:LIM:UPP 2.5", "math"),
    ({"limit_on": False}, _g("math", "limit_on"), False, "CALC:LIM:STAT OFF", "math"),
    (
        {"hist_points": 200, "hist_on": True},
        _g("math", "hist_points"),
        200,
        "CALC:TRAN:HIST:POIN 200",
        "math",
    ),
    (
        {"hist_auto": False, "hist_low": -1.0, "hist_high": 1.0},
        _g("math", "hist_low"),
        -1.0,
        "CALC:TRAN:HIST:RANG:LOW -1",
        "math",
    ),
    ({"hist_high": 1.0}, _g("math", "hist_high"), 1.0, "CALC:TRAN:HIST:RANG:UPP 1", "math"),
    ({"hist_auto": True}, _g("math", "hist_auto"), True, "CALC:TRAN:HIST:RANG:AUTO ON", "math"),
    ({"hist_on": False}, _g("math", "hist_on"), False, "CALC:TRAN:HIST:STAT OFF", "math"),
    ({"view": "TCH"}, _g("display", "view"), "TCH", "DISP:VIEW TCH", "disp"),
    ({"view": "HIST"}, _g("display", "view"), "HIST", "DISP:VIEW HIST", "disp"),
    ({"view": "MET"}, _g("display", "view"), "MET", "DISP:VIEW MET", "disp"),
    ({"view": "NUM"}, _g("display", "view"), "NUM", "DISP:VIEW NUM", "disp"),
    ({"text": "BENCH BUDDY"}, _g("display", "text"), "BENCH BUDDY", "DISP:TEXT", "disp"),
    ({"text": ""}, _g("display", "text"), "", "DISP:TEXT:CLE", "disp"),
]

VERIFY = [(2500, "DCV", _function("VOLT:DC"))]
for _changes, _getter, _want, _label, _kind in _STEPS:
    _sender = {"cfg": _config, "trig": _trigger, "math": _math, "disp": _display}[_kind]
    # 1800/300 was too tight: set_config/set_trigger stand the keepalive down,
    # apply the change and then re-read the whole State object, which takes
    # about a second on its own, so the check could land before the fresh state
    # had been published and report the *previous* value as a failure.
    VERIFY.append((2600, _label, _sender(_changes)))
    VERIFY.append((700, "check", _expect(_getter, _want, _label)))

VERIFY += [
    (2500, "ACV", _function("VOLT:AC")),
    (1800, "VOLT:AC:BAND 3", _config({"band": 3})),
    (300, "check", _expect(_g("band"), 3.0, "VOLT:AC:BAND 3")),
    (1800, "VOLT:AC:BAND 200", _config({"band": 200})),
    (300, "check", _expect(_g("band"), 200.0, "VOLT:AC:BAND 200")),
    (2500, "FREQ", _function("FREQ")),
    (1800, "FREQ:APER 1", _config({"aperture": 1.0})),
    (300, "check", _expect(_g("aperture"), 1.0, "FREQ:APER 1")),
    (1800, "FREQ:APER 0.01", _config({"aperture": 0.01})),
    (300, "check", _expect(_g("aperture"), 0.01, "FREQ:APER 0.01")),
    (2500, "PER", _function("PER")),
    (1800, "PER:APER 0.1", _config({"aperture": 0.1})),
    (300, "check", _expect(_g("aperture"), 0.1, "PER:APER 0.1")),
    (2500, "TEMP", _function("TEMP")),
    (1800, "TEMP:TRAN:TYPE THER", _config({"temp_type": "THER"})),
    (300, "check", _expect(_g("temp", "type"), "THER", "TEMP:TRAN:TYPE THER")),
    (1800, "UNIT:TEMP F", _config({"temp_unit": "F"})),
    (300, "check", _expect(_g("temp", "unit"), "F", "UNIT:TEMP F")),
    (1800, "UNIT:TEMP C", _config({"temp_unit": "C"})),
    (300, "check", _expect(_g("temp", "unit"), "C", "UNIT:TEMP C")),
    (1800, "TEMP:TRAN:TYPE FRTD", _config({"temp_type": "FRTD"})),
    (300, "check", _expect(_g("temp", "type"), "FRTD", "TEMP:TRAN:TYPE FRTD")),
    (1800, "TEMP:TRAN:RTD:RES 100", _config({"rtd_res": 100.0})),
    (300, "check", _expect(_g("temp", "rtd_res"), 100.0, "TEMP:TRAN:RTD:RES 100")),
    (1800, "TEMP:NPLC 1", _config({"nplc": 1.0})),
    (300, "check", _expect(_g("nplc"), 1.0, "TEMP:NPLC 1")),
    (2500, "2W", _function("RES")),
    (1800, "RES:RANG 10k", _config({"range": 10000.0, "range_auto": False})),
    (300, "check", _expect(_g("range"), 10000.0, "RES:RANG 10k")),
    (2500, "4W", _function("FRES")),
    (1800, "FRES:RANG 100k", _config({"range": 100000.0, "range_auto": False})),
    (300, "check", _expect(_g("range"), 100000.0, "FRES:RANG 100k")),
    (2500, "CAP", _function("CAP")),
    (1800, "CAP:RANG 1u", _config({"range": 1e-6, "range_auto": False})),
    (300, "check", _expect(_g("range"), 1e-6, "CAP:RANG 1u")),
    (2500, "DCI", _function("CURR:DC")),
    (1800, "CURR:DC:RANG 10m", _config({"range": 0.01, "range_auto": False})),
    (300, "check", _expect(_g("range"), 0.01, "CURR:DC:RANG 10m")),
    (2500, "ACI", _function("CURR:AC")),
    (1800, "CURR:AC:RANG 1", _config({"range": 1.0, "range_auto": False})),
    (300, "check", _expect(_g("range"), 1.0, "CURR:AC:RANG 1")),
    (2500, "DCV back", _function("VOLT:DC")),
    (1800, "restore auto", _config({"range_auto": True})),
    (500, "summary", _checks_report),
]


# --------------------------------------------------- closing-out verification

_ECHO = {"wired": False}


def _echo(window, shoot) -> None:
    """Print what the application tells its user, so nothing is inferred."""
    if _ECHO["wired"]:
        return
    _ECHO["wired"] = True
    bridge = window.bridge.worker

    def scpi(result) -> None:
        print(
            "  CONSOLE %r -> response=%r error=%r (%.0f ms)"
            % (
                result.get("cmd") or result.get("command"),
                result.get("response"),
                result.get("error"),
                result.get("elapsed_ms") or 0.0,
            ),
            flush=True,
        )

    bridge.scpiReady.connect(scpi)
    bridge.errorRaised.connect(lambda t: print(f"  ERROR   {t}", flush=True))
    bridge.noticeRaised.connect(lambda t: print(f"  NOTICE  {t}", flush=True))
    bridge.localReady.connect(lambda r: print(f"  LOCAL   {r}", flush=True))
    bridge.frameReady.connect(
        lambda data, stamp: print(
            f"  FRAME   {len(data)} bytes of PNG, captured at {stamp:.3f}",
            flush=True,
        )
    )
    print("  echoing console / error / notice / local / frame signals", flush=True)


def _errq(window, shoot) -> None:
    """SYST:ERR? through the application, as the protocol asks between steps."""
    window.bridge.passthrough.emit("SYST:ERR?")


def _link_report(window, shoot) -> None:
    state = window.state or {}
    beat = state.get("heartbeat") or {}
    print(
        "  link: transport=%r crash_safe=%r deadman=%r count=%r note=%r"
        % (
            state.get("transport"),
            state.get("crash_safe"),
            beat.get("deadman"),
            beat.get("count"),
            state.get("transport_note"),
        ),
        flush=True,
    )
    print(
        "  keepalive label: %r   tooltip: %r"
        % (
            window.keepalive_label.text(),
            window.keepalive_label.toolTip().replace("\n\n", " | "),
        ),
        flush=True,
    )


def _streaming_still_on(window, shoot) -> None:
    state = window.state or {}
    ok = bool(state.get("streaming"))
    CHECKS["pass" if ok else "fail"] += 1
    print(
        "  %s  the run survived the console command: streaming=%r link=%r"
        % ("PASS" if ok else "FAIL", state.get("streaming"), window.link_label.text()),
        flush=True,
    )


def _local(window, shoot) -> None:
    window.bridge.returnToLocal.emit()


CLOSEOUT = [
    (300, "echo signals", _echo),
    (1500, "connected", _report),
    (500, "link", _link_report),
    (2000, "DCV", _function("VOLT:DC")),
    (1500, "SYST:ERR?", _errq),
    # The user's trigger setup must survive the idle keepalive, which drives
    # TRIG:COUN / SAMP:COUN for its own purposes.
    (2800, "user sets Count = 10", _trigger({"count": 10})),
    (2800, "user sets Samples = 4", _trigger({"samples": 4})),
    (800, "check count", _expect(_g("trigger", "count"), 10, "TRIG:COUN 10")),
    (800, "check samples", _expect(_g("trigger", "samples"), 4, "SAMP:COUN 4")),
    (18000, "18 s of idle keepalive", _report),
    (600, "count after keepalive", _expect(_g("trigger", "count"), 10, "TRIG:COUN still 10")),
    (600, "samples after keepalive", _expect(_g("trigger", "samples"), 4, "SAMP:COUN still 4")),
    (600, "link", _link_report),
    (1500, "SYST:ERR?", _errq),
    # One on-demand screen grab (rule 4), while idle.
    (1000, "capture screen", _screen(True)),
    (3000, "SYST:ERR?", _errq),
    # The console, with a run in progress: the run must not be stopped.
    (2000, "start stream", _stream(True)),
    (5000, "running", _report),
    (500, "console: FOO:BAR? (unsupported query)", _type_scpi("FOO:BAR?")),
    (8000, "did the run survive?", _streaming_still_on),
    (500, "report", _report),
    (500, "console: *IDN? (did the link recover?)", _type_scpi("*IDN?")),
    (3000, "report", _report),
    (500, "did the run survive that too?", _streaming_still_on),
    (500, "one grab while streaming", _screen(True)),
    (4000, "report", _report),
    (2000, "stop stream", _stream(False)),
    (2500, "SYST:ERR?", _errq),
    (2000, "restore trigger to Count = 1", _trigger({"count": 1, "samples": 1})),
    (2000, "summary", _checks_report),
    (500, "Return to Local", _local),
    (3000, "after local", _report),
    (500, "link", _link_report),
]


def _dump(*path):
    """Print one branch of the State object, for chasing a round-trip failure."""

    def go(window, shoot) -> None:
        node = window.state or {}
        for key in path:
            node = (node or {}).get(key)
        print(f"  state{list(path)} = {node!r}", flush=True)

    return go


PROBE = [
    (300, "echo signals", _echo),
    (1500, "connected", _report),
    (2500, "DCV", _function("VOLT:DC")),
    # --- 1. the 1000 V range
    (2000, "range 0.1, auto off", _config({"range": 0.1, "range_auto": False})),
    (1000, "state", _dump("range")),
    (2000, "range 1000", _config({"range": 1000.0})),
    (1500, "state", _dump("range")),
    (500, "ask the instrument itself", _type_scpi("VOLT:DC:RANG?")),
    (2000, "SYST:ERR?", _errq),
    (2000, "range 100", _config({"range": 100.0})),
    (1500, "state", _dump("range")),
    (500, "ask the instrument itself", _type_scpi("VOLT:DC:RANG?")),
    (2000, "range 1000 again", _config({"range": 1000.0})),
    (1500, "state", _dump("range")),
    (500, "ask the instrument itself", _type_scpi("VOLT:DC:RANG?")),
    (2000, "SYST:ERR?", _errq),
    (2000, "auto range back", _config({"range_auto": True})),
    # --- 2. TRIG:COUN 7
    (2500, "trigger count 7", _trigger({"count": 7})),
    (1500, "state", _dump("trigger")),
    (500, "ask the instrument itself", _type_scpi("TRIG:COUN?")),
    (2500, "trigger count 7 again", _trigger({"count": 7})),
    (1500, "state", _dump("trigger")),
    (2000, "SYST:ERR?", _errq),
    (2000, "trigger count 1", _trigger({"count": 1})),
    # --- 3. NULL:VAL:AUTO
    (2500, "null value 0.125 + null on", _math({"null_value": 0.125, "null_on": True})),
    (1500, "state", _dump("math")),
    (2500, "null auto on", _math({"null_auto": True})),
    (1500, "state", _dump("math")),
    (500, "ask the instrument itself", _type_scpi("VOLT:DC:NULL:VAL:AUTO?")),
    (2500, "read it again a moment later", _dump("math")),
    (500, "ask again", _type_scpi("VOLT:DC:NULL:VAL:AUTO?")),
    (2000, "SYST:ERR?", _errq),
    (2000, "null off", _math({"null_on": False, "null_auto": False})),
    (1500, "state", _dump("math")),
]


def _gap_report(window, shoot) -> None:
    """Prove the overloaded stretch is a hole in the trace, not a straight line."""
    import numpy as np

    chart = window.chart_panel.chart
    n = chart._n
    v = chart._vs[:n]
    bad = np.isnan(v)
    runs = []
    start = None
    for i, flag in enumerate(bad):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i - start))
            start = None
    if start is not None:
        runs.append((start, n - start))
    print(
        "  chart: %d points, %d overloaded; NaN runs (start,len) = %s"
        % (n, int(bad.sum()), runs[:5]),
        flush=True,
    )
    finite_before = int((~bad[: runs[0][0]]).sum()) if runs else 0
    finite_after = (
        int((~bad[runs[-1][0] + runs[-1][1] :]).sum()) if runs else 0
    )
    ok = bool(runs) and finite_before > 10 and finite_after > 10
    CHECKS["pass" if ok else "fail"] += 1
    print(
        "  %s  valid -> overload -> valid: %d finite, then a %d-sample hole, "
        "then %d finite"
        % (
            "PASS" if ok else "FAIL",
            finite_before,
            runs[0][1] if runs else 0,
            finite_after,
        ),
        flush=True,
    )


# A mixed trace needs the *same* function throughout — changing function
# changes the unit and rightly clears the chart.  ACV on open leads picks up
# a few hundred mV of mains hum, so the 10 V range reads it happily and the
# 100 mV range overloads on it: valid -> OVLD -> valid without touching the
# leads.
GAP = [
    (1500, "connected", _report),
    (2500, "DCV", _function("VOLT:DC")),
    (2500, "10 V fixed, NPLC 0.02", _config({"range": 10.0, "range_auto": False, "nplc": 0.02})),
    (2000, "start stream", _stream(True)),
    (8000, "valid readings", _report),
    (500, "shot valid", _shot("gap-1-valid")),
    (2500, "force overload: same function, 100 mV range", _config({"range": 0.1})),
    (9000, "overloading", _report),
    (500, "shot overload", _shot("gap-2-overload")),
    (2500, "back to the 10 V range", _config({"range": 10.0})),
    (9000, "valid again", _report),
    (500, "chart contents", _gap_report),
    (500, "shot the gap", _shot("gap-3-gap")),
    (2000, "stop", _stream(False)),
    (2000, "auto range back", _config({"range_auto": True})),
    (500, "summary", _checks_report),
]


def _readout_report(window, shoot) -> None:
    state = window.state or {}
    math = state.get("math") or {}
    print(
        "  readout unit=%r  scale_on=%r func=%r db_ref=%r dbm_ref=%r "
        "null_on=%r null_value=%r"
        % (
            state.get("unit"),
            math.get("scale_on"),
            math.get("scale_func"),
            math.get("db_ref"),
            math.get("dbm_ref"),
            math.get("null_on"),
            math.get("null_value"),
        ),
        flush=True,
    )


# The last run of the session: exercise the scaling maths visibly, then put
# everything back the way a user would want to find it and hand the meter over.
FINISH = [
    (300, "echo signals", _echo),
    (1500, "connected", _report),
    (2500, "DCV", _function("VOLT:DC")),
    (2600, "10 V fixed, NPLC 1", _config({"range": 10.0, "range_auto": False, "nplc": 1.0})),
    (2600, "null 0.001 V on", _math({"null_value": 0.001, "null_on": True})),
    (2000, "single reading", lambda w, s: w.bridge.single.emit()),
    (1200, "readout", _readout_report),
    (500, "shot null", _shot("fin-1-null")),
    (2600, "null off", _math({"null_on": False})),
    (2600, "dB scaling, ref 1 V", _math({"scale_func": "DB", "db_ref": 1.0, "scale_on": True})),
    (2000, "single reading", lambda w, s: w.bridge.single.emit()),
    (1200, "readout", _readout_report),
    (500, "shot dB", _shot("fin-2-db")),
    (2600, "dBm scaling, ref 50 ohm", _math({"scale_func": "DBM", "dbm_ref": 50.0})),
    (2000, "single reading", lambda w, s: w.bridge.single.emit()),
    (1200, "readout", _readout_report),
    (500, "shot dBm", _shot("fin-3-dbm")),
    (2600, "scaling off", _math({"scale_on": False})),
    # --- put the bench back
    (2600, "stats and limits off", _math({"stats_on": False, "limit_on": False})),
    (2600, "histogram off", _math({"hist_on": False})),
    (2600, "display back to Number", _display({"view": "NUM"})),
    (2600, "auto range, NPLC 10, autozero on, 10M input",
     _config({"range_auto": True, "nplc": 10.0, "azero": "ON", "impedance": "10M"})),
    (2600, "trigger IMM, positive slope, auto delay",
     _trigger({"source": "IMM", "slope": "POS", "delay_auto": True})),
    (2600, "trigger count 1, samples 1", _trigger({"count": 1, "samples": 1})),
    (2000, "SYST:ERR?", _errq),
    (2500, "final state", _report),
    (500, "final link", _link_report),
    (500, "shot final", _shot("fin-4-final")),
]


RESTORE = [
    (300, "echo signals", _echo),
    (1500, "connected", _report),
    (2600, "DCV", _function("VOLT:DC")),
    (2600, "scaling off", _math({"scale_on": False})),
    (2600, "null off", _math({"null_on": False, "null_auto": False})),
    (2600, "stats and limits off", _math({"stats_on": False, "limit_on": False})),
    (2600, "histogram off", _math({"hist_on": False})),
    (2600, "display back to Number", _display({"view": "NUM"})),
    (2600, "auto range, NPLC 10, autozero on, 10M input",
     _config({"range_auto": True, "nplc": 10.0, "azero": "ON", "impedance": "10M"})),
    (2600, "trigger IMM, positive slope, auto delay",
     _trigger({"source": "IMM", "slope": "POS", "delay_auto": True})),
    (2600, "trigger count 1, samples 1", _trigger({"count": 1, "samples": 1})),
    (2000, "SYST:ERR?", _errq),
    (2500, "final state", _report),
    (600, "final math", _readout_report),
    (600, "final trigger", _dump("trigger")),
    (600, "final link", _link_report),
    (500, "shot final", _shot("restore-final")),
]


SCRIPTS = {
    "restore": RESTORE,
    "finish": FINISH,
    "gap": GAP,
    "probe": PROBE,
    "closeout": CLOSEOUT,
    "verify": VERIFY,
    "shutdown": SHUTDOWN,
    "readout": READOUT,
    "scale": SCALE,
    "endurance": ENDURANCE,
    "console": CONSOLE,
    "tour": TOUR,
    "functions": [(1500, "connected", _report)] + FUNCTIONS,
    "config": CONFIG,
    "overload": OVERLOAD,
}


if __name__ == "__main__":
    raise SystemExit(main())
