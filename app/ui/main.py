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

"""The application window.

Three zones, as SPEC.md section 6 and ARCHITECTURE.md section 6 describe them: a
top bar carrying identity and link state, the function rail on the left, the
hero readout with its softkey strip and tabs in the centre, and the screen
capture with statistics and limits on the right.

All painting is driven by one 30 Hz timer.  Readings arrive in batches at up to
~740/s; they are buffered as they arrive and never dropped, but the readout,
the chart and the log table each repaint at most once per tick.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..instrument import install_safety_net
from ..models import PRIMARY_MODEL, unverified_warning
from . import theme
from .bridge import Bridge
from .chart import ChartPanel
from .connect import (
    ConnectDialog,
    InstrumentLibrary,
    SavedInstrument,
    TRANSPORT_KEYS,
    label_from_identity,
    validate_address,
)
from .console import ConsolePanel
from .histogram import HistogramPanel
from .logtab import LogPanel
from .mirror import CapturePanel
from .readout import ReadoutWidget
from .strip import ControlStrip, FunctionRail
from .system import SystemPanel
from .units import common_si, fmt_scaled, fmt_state, is_num

PAINT_HZ = 30
# How long the window waits for the worker to hand the instrument back before
# closing anyway and saying so.  The handover is a few writes and a socket
# close, so this is a long stop, not an expected one.
SHUTDOWN_TIMEOUT_MS = 30000
RAIL_COLLAPSE_PX = 1000
FUNCTION_RAIL_COLLAPSE_PX = 840
# The SCPI raw socket port.  It is not per-instrument: the raw socket is always
# 5025 on this family, VXI-11 finds its own port through the portmapper, and
# --port is still there for anything sitting behind a forwarder.
DEFAULT_PORT = 5025


def _reading_kind(state: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """What the readings *are*: the function, and the unit they arrive in.

    Two states with the same kind can share a chart, a log table and a y axis.
    Anything else — a different function, or scaling switched on, off or
    between dB and dBm — makes the samples already buffered a different
    quantity from the ones about to arrive.
    """
    if not state:
        return ("", "")
    math_state = state.get("math") or {}
    scaling = ""
    if math_state.get("scale_on"):
        token = str(math_state.get("scale_func") or "").strip().upper()
        # NULL is a subtraction in the function's own unit, so it does not
        # change the quantity; only the logarithmic functions do.
        scaling = token if token in ("DB", "DBM") else ""
    return (str(state.get("func") or ""), scaling)


def _reading_change_text(was: Tuple[str, str], now: Tuple[str, str]) -> str:
    """One line naming what changed, for the status bar."""
    if was[0] != now[0]:
        return f"Function changed to {now[0] or 'none'}"
    if now[1]:
        return f"Scaling switched to {now[1]}"
    return f"{was[1] or 'Scaling'} scaling switched off"


class LiveDot(QWidget):
    """The one piece of motion in the interface: a slow pulse while running."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(12, 12))
        self._live = False
        self._phase = 0.0

    def set_live(self, live: bool) -> None:
        if live != self._live:
            self._live = live
            self.update()

    def tick(self) -> None:
        if self._live:
            self._phase = (self._phase + 1.0 / PAINT_HZ) % 2.0
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        colour = QColor(theme.SIGNAL if self._live else theme.DIM)
        if self._live:
            alpha = 0.55 + 0.45 * (0.5 + 0.5 * math.cos(self._phase * math.pi))
            halo = QColor(colour)
            halo.setAlphaF(alpha * 0.35)
            painter.setPen(Qt.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(self.rect().center(), 6, 6)
            colour.setAlphaF(alpha)
        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(self.rect().center(), 3, 3)
        painter.end()


def _card(title: str, node: str = "") -> Tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Panel")
    box = QVBoxLayout(frame)
    box.setContentsMargins(12, 10, 12, 12)
    box.setSpacing(6)
    header = QHBoxLayout()
    heading = QLabel(title)
    heading.setObjectName("SectionLabel")
    heading.setFont(theme.label())
    header.addWidget(heading)
    header.addStretch(1)
    if node:
        caption = QLabel(node)
        caption.setObjectName("Caption")
        caption.setFont(theme.caption())
        header.addWidget(caption)
    box.addLayout(header)
    return frame, box


class StatsCard(QWidget):
    """The instrument's own CALC:AVER: statistics."""

    toggled = Signal(bool)
    clearRequested = Signal()

    FIELDS = (
        ("avg", "AVERAGE"),
        ("min", "MINIMUM"),
        ("max", "MAXIMUM"),
        ("ptp", "PEAK-PEAK"),
        ("sdev", "STD DEV"),
        ("count", "COUNT"),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.frame, box = _card("STATISTICS", "CALC:AVER")
        layout.addWidget(self.frame)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.enable = QPushButton("Off")
        self.enable.setObjectName("Toggle")
        self.enable.setCheckable(True)
        self.enable.toggled.connect(self._toggled)
        controls.addWidget(self.enable)
        self.clear = QPushButton("Clear")
        self.clear.clicked.connect(self.clearRequested)
        controls.addWidget(self.clear)
        controls.addStretch(1)
        box.addLayout(controls)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(1, 1)
        self.values: Dict[str, QLabel] = {}
        for row, (key, label) in enumerate(self.FIELDS):
            name = QLabel(label)
            name.setObjectName("IdentKey")
            value = QLabel("—")
            value.setObjectName("Value")
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(name, row, 0)
            grid.addWidget(value, row, 1)
            self.values[key] = value
        box.addLayout(grid)
        self._syncing = False

    def _toggled(self, on: bool) -> None:
        self.enable.setText("On" if on else "Off")
        if not self._syncing:
            self.toggled.emit(on)

    def set_stats(self, stats: Dict[str, Any], state: Optional[Dict[str, Any]]) -> None:
        numeric = [stats.get(key) for key, _ in self.FIELDS if key != "count"]
        mult, label = common_si(numeric, state)
        for key, _label in self.FIELDS:
            value = stats.get(key)
            if key == "count":
                text = "—" if value is None else f"{int(value):,}"
            else:
                text = fmt_scaled(value, mult, label, 7)
            self.values[key].setText(text)

    def clear_values(self) -> None:
        """Forget the figures — they belong to one instrument's acquisition.

        Not ``clear``: that name is already the Clear *button* on this card.
        """
        for value in self.values.values():
            value.setText("—")
        self._syncing = True
        self.enable.setChecked(False)
        self.enable.setText("Off")
        self._syncing = False

    def apply_state(self, state: Dict[str, Any]) -> None:
        on = bool((state.get("math") or {}).get("stats_on"))
        self._syncing = True
        self.enable.setChecked(on)
        self.enable.setText("On" if on else "Off")
        self._syncing = False


class LimitsCard(QWidget):
    """CALC:LIM — the pass/fail verdict the instrument reports."""

    toggled = Signal(bool)
    boundsChanged = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from .strip import NumberField

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.frame, box = _card("LIMITS", "CALC:LIM")
        layout.addWidget(self.frame)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.enable = QPushButton("Off")
        self.enable.setObjectName("Toggle")
        self.enable.setCheckable(True)
        self.enable.toggled.connect(self._toggled)
        controls.addWidget(self.enable)
        controls.addStretch(1)
        self.verdict = QLabel("Idle")
        self.verdict.setObjectName("ValueBig")
        controls.addWidget(self.verdict)
        box.addLayout(controls)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        low_label = QLabel("LOWER")
        low_label.setObjectName("IdentKey")
        high_label = QLabel("UPPER")
        high_label.setObjectName("IdentKey")
        self.low = NumberField(110)
        self.high = NumberField(110)
        self.low.committed.connect(lambda v: self.boundsChanged.emit({"limit_low": v}))
        self.high.committed.connect(lambda v: self.boundsChanged.emit({"limit_high": v}))
        grid.addWidget(low_label, 0, 0)
        grid.addWidget(self.low, 0, 1)
        grid.addWidget(high_label, 1, 0)
        grid.addWidget(self.high, 1, 1)
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)

        caption = QLabel("CALC:LIM:LOW · CALC:LIM:UPP")
        caption.setObjectName("Caption")
        caption.setFont(theme.caption())
        box.addWidget(caption)
        self._syncing = False

    def _toggled(self, on: bool) -> None:
        self.enable.setText("On" if on else "Off")
        if not self._syncing:
            self.toggled.emit(on)

    def set_verdict(self, status: Optional[str]) -> None:
        text, colour = {
            "pass": ("Pass", theme.OK),
            "fail_low": ("Below limit", theme.FAIL),
            "fail_high": ("Above limit", theme.FAIL),
        }.get(status or "", ("Idle", theme.DIM))
        self.verdict.setText(text)
        self.verdict.setStyleSheet(f"color: {colour};")

    def apply_state(self, state: Dict[str, Any]) -> None:
        math_state = state.get("math") or {}
        on = bool(math_state.get("limit_on"))
        self._syncing = True
        self.enable.setChecked(on)
        self.enable.setText("On" if on else "Off")
        self._syncing = False
        self.low.show_value(math_state.get("limit_low"))
        self.high.show_value(math_state.get("limit_high"))
        if not on:
            self.set_verdict(None)


class MainWindow(QMainWindow):
    """The Bench Buddy main window."""

    def __init__(
        self,
        library: InstrumentLibrary,
        port: int = DEFAULT_PORT,
        connect_index: int = -1,
        session_transport: str = "",
        session_finite: bool = False,
    ) -> None:
        """Build the window.  *connect_index* is the entry to open at startup.

        Pass -1 to start disconnected; :meth:`show_connect_dialog` is then the
        only route to a link, which is what happens on a first run with nothing
        saved and no ``--instrument``.

        *session_transport* and *session_finite* are the ``--transport`` and
        ``--finite-trigger-count`` flags.  **They apply to this run only.**
        They used to be written into the matched library entry and saved, so a
        single ``--finite-trigger-count`` run left that meter permanently on
        the finite renewed count — a blanked reading every few seconds on its
        front panel, for ever, with nothing on screen explaining why.  They
        are held here instead and applied to the entry the command line named,
        which is what :meth:`_link_options` resolves.
        """
        super().__init__()
        self.setWindowTitle("Bench Buddy")
        self.setMinimumSize(1100, 720)
        self.resize(1500, 940)

        self.library = library
        self.port = port
        self.host = ""
        self.state: Optional[Dict[str, Any]] = None
        self._rate_events: Deque[Tuple[float, int]] = deque()
        self._closing = False
        self._shutdown_done = False
        self._shutdown_ok: Optional[bool] = None
        # Connection state, honestly reported: "offline", "connecting",
        # "connected" or "disconnecting".  The top-bar button, the menus and
        # the status line all read it rather than each keeping their own idea.
        self._phase = "offline"
        # The library *entry* the current link belongs to, and the index of
        # the one waiting for the previous worker to finish handing its
        # instrument back.  The connected one is held as the object rather
        # than as an index because the connection dialog can add, remove and
        # reorder entries underneath it: an index went stale the moment an
        # entry above it was removed, and then named the wrong meter in the
        # title, marked the wrong row as connected, and — worst — wrote this
        # instrument's "MODEL SERIAL" label onto a *different* saved entry,
        # which is permanent corruption of the library.  See _connected_index.
        self._connected_entry: Optional[SavedInstrument] = None
        self._pending_index = -1
        # Each handover arms a watchdog; only the newest one may act.  Earlier
        # timers cannot be cancelled, so they are stamped instead.
        self._handover_token = 0
        self._link_was_up = False
        # Set when a handback did not complete, so the disconnect message does
        # not claim the front panel is free-running again when it may not be.
        self._handback_failed = False
        # Set while the picker and the Instruments menu are being rebuilt, so
        # the rebuild's own index changes are not read back as user choices.
        self._refreshing_controls = False
        # What to say, and whether to reopen the dialog, once the worker that
        # is on its way out has actually finished.
        self._offline_message = ""
        self._offline_banner = ""
        self._reopen_dialog = False

        # Session-scoped command-line overrides, and the entry they were given
        # for — held as the object, so editing the library cannot make them
        # apply to a different meter.
        self._session_transport = (
            session_transport if session_transport in TRANSPORT_KEYS else ""
        )
        self._session_finite = bool(session_finite)
        self._session_entry: Optional[SavedInstrument] = None
        if 0 <= connect_index < len(library) and (
            self._session_transport or self._session_finite
        ):
            self._session_entry = library.entries[connect_index]

        self.bridge = Bridge(self)
        self._build()
        self._wire()
        self._refresh_instrument_controls()
        self._set_phase("offline")

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(int(1000 / PAINT_HZ))

        if library.load_error:
            self._show_banner(library.load_error, "warn")
        if 0 <= connect_index < len(library):
            QTimer.singleShot(0, lambda: self.connect_to_index(connect_index))
        else:
            self.status.showMessage(
                "No instrument connected — choose one to connect to."
            )
            QTimer.singleShot(0, self.show_connect_dialog)

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        self._build_menus()
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.rail = FunctionRail()
        body.addWidget(self.rail)

        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(14, 12, 14, 12)
        centre_layout.setSpacing(10)

        self.banner = self._build_banner()
        centre_layout.addWidget(self.banner)

        self.readout = ReadoutWidget()
        centre_layout.addWidget(self.readout)

        self.strip = ControlStrip()
        centre_layout.addWidget(self.strip)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.chart_panel = ChartPanel()
        self.histogram_panel = HistogramPanel()
        self.log_panel = LogPanel()
        self.console_panel = ConsolePanel()
        self.system_panel = SystemPanel()
        self.tabs.addTab(self.chart_panel, "Chart")
        self.tabs.addTab(self.histogram_panel, "Histogram")
        self.tabs.addTab(self.log_panel, "Log")
        self.tabs.addTab(self.console_panel, "SCPI")
        self.tabs.addTab(self.system_panel, "System")
        centre_layout.addWidget(self.tabs, 1)

        body.addWidget(centre, 1)
        body.addWidget(self._build_right_rail())
        outer.addLayout(body, 1)

        self.status = self.statusBar()
        self.status.setSizeGripEnabled(False)

    def _build_menus(self) -> None:
        """A minimal menu bar, because that is where users look for Connect.

        There was none at all before: the address could only be given on the
        command line, so there was nothing for a menu to do.
        """
        bar = self.menuBar()
        bar.setNativeMenuBar(False)  # keep the designed bar on macOS too

        file_menu = bar.addMenu("&File")
        self.connect_action = QAction("&Connect…", self)
        self.connect_action.setShortcut(QKeySequence("Ctrl+O"))
        self.connect_action.setStatusTip(
            "Choose a saved instrument, or add one, and connect to it"
        )
        self.connect_action.triggered.connect(self.show_connect_dialog)
        file_menu.addAction(self.connect_action)

        self.disconnect_action = QAction("&Disconnect", self)
        self.disconnect_action.setShortcut(QKeySequence("Ctrl+D"))
        self.disconnect_action.setStatusTip(
            "Restore the trigger setup, return the instrument to local with "
            "SYST:LOC and close the link"
        )
        self.disconnect_action.triggered.connect(self.disconnect_instrument)
        file_menu.addAction(self.disconnect_action)
        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        # Stated rather than inferred. On macOS Qt guesses a menu role from the
        # action's text and relocates it into the application menu; saying which
        # role this is keeps Cmd+Q behaving the same way regardless of how the
        # label is worded or translated.
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.setStatusTip(
            "Hand the instrument back and close the application"
        )
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # The menu that gets used daily: every saved meter, one click away.
        # Its contents are rebuilt from the library by
        # _refresh_instrument_controls, so nothing is held on to here.
        self.instruments_menu = bar.addMenu("&Instruments")

        help_menu = bar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(18)

        title = QLabel("34461A")
        title.setFont(theme.readout(17, QFont.DemiBold))
        title.setStyleSheet(f"color: {theme.PHOSPHOR};")
        layout.addWidget(title)

        subtitle = QLabel("Bench Buddy")
        subtitle.setFont(theme.sans(12))
        subtitle.setStyleSheet(f"color: {theme.DIM};")
        layout.addWidget(subtitle)

        layout.addSpacing(8)
        self.ident_fields: Dict[str, QLabel] = {}
        for key, label in (
            ("serial", "SERIAL"),
            ("firmware", "FIRMWARE"),
            ("ip", "ADDRESS"),
        ):
            block = QVBoxLayout()
            block.setSpacing(0)
            name = QLabel(label)
            name.setObjectName("IdentKey")
            value = QLabel("—")
            value.setObjectName("Ident")
            block.addWidget(name)
            block.addWidget(value)
            layout.addLayout(block)
            self.ident_fields[key] = value

        layout.addStretch(1)

        self.rate_label = QLabel("0.0 rdg/s")
        self.rate_label.setObjectName("ValueBig")
        layout.addWidget(self.rate_label)

        self.live_dot = LiveDot()
        layout.addWidget(self.live_dot)

        link_block = QVBoxLayout()
        link_block.setSpacing(0)
        self.link_label = QLabel("Connecting")
        self.link_label.setObjectName("Value")
        self.link_label.setMinimumWidth(150)
        link_block.addWidget(self.link_label)
        # Rule 1 made visible: while the app holds the link the instrument is
        # in remote, and the front panel only stays alive because the keepalive
        # is measuring.  If it ever stops, the user is told here and why,
        # rather than being left to wonder why the bench meter looks dead.
        self.keepalive_label = QLabel("")
        self.keepalive_label.setObjectName("Caption")
        self.keepalive_label.setMinimumWidth(150)
        link_block.addWidget(self.keepalive_label)
        layout.addLayout(link_block)

        # The connection control sits next to the state it reports on, so
        # pressing Connect is never a guess about which meter it will open.
        # The picker chooses; the button acts and shows the phase.
        picker_block = QVBoxLayout()
        picker_block.setSpacing(0)
        picker_caption = QLabel("INSTRUMENT")
        picker_caption.setObjectName("IdentKey")
        picker_block.addWidget(picker_caption)
        self.instrument_picker = QComboBox()
        self.instrument_picker.setObjectName("InstrumentPicker")
        self.instrument_picker.setMinimumWidth(168)
        self.instrument_picker.setMaximumWidth(230)
        self.instrument_picker.setToolTip(
            "The saved instrument the Connect button will open. Use "
            "File ▸ Connect… to add, rename or remove entries."
        )
        self.instrument_picker.currentIndexChanged.connect(self._picker_changed)
        picker_block.addWidget(self.instrument_picker)
        layout.addLayout(picker_block)

        self.link_button = QPushButton("Connect")
        self.link_button.setObjectName("LinkButton")
        self.link_button.clicked.connect(self._link_button_pressed)
        layout.addWidget(self.link_button)

        self.local_button = QPushButton("Return to Local")
        self.local_button.setToolTip(
            "Hand the instrument back to its front panel with SYST:LOC. "
            "The trigger setup is restored first, so the panel free-runs again."
        )
        self.local_button.clicked.connect(self._return_to_local)
        layout.addWidget(self.local_button)
        return bar

    def _build_banner(self) -> QWidget:
        banner = QFrame()
        banner.setObjectName("Panel")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(10)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        layout.addWidget(self.banner_text, 1)
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(lambda: banner.setVisible(False))
        layout.addWidget(dismiss)
        banner.setVisible(False)
        return banner

    def _build_right_rail(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("RightRail")
        rail.setFixedWidth(322)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.capture = CapturePanel()
        layout.addWidget(self.capture)

        self.stats_card = StatsCard()
        layout.addWidget(self.stats_card)

        self.limits_card = LimitsCard()
        layout.addWidget(self.limits_card)
        layout.addStretch(1)

        version = QLabel(f"version {__version__}")
        version.setObjectName("Caption")
        layout.addWidget(version)
        self.right_rail = rail
        return rail

    # ------------------------------------------------------------------ wiring

    def _wire(self) -> None:
        # Every result comes from the bridge, never from the worker: the worker
        # is replaced whenever the user connects to a different instrument, and
        # a connection made here would be left pointing at the old one.
        source = self.bridge
        source.stateReady.connect(self._on_state)
        source.dataReady.connect(self._on_data)
        source.statsReady.connect(self._on_stats)
        source.limitReady.connect(self._on_limit)
        source.errorRaised.connect(self._on_error)
        source.noticeRaised.connect(self._on_notice)
        source.systemReady.connect(self._on_system)
        source.histogramReady.connect(self._on_histogram)
        source.scpiReady.connect(self.console_panel.show_result)
        source.singleReady.connect(self._on_single)
        source.selftestReady.connect(self.system_panel.set_selftest)
        source.frameReady.connect(self._on_frame)
        source.exportReady.connect(self._on_export)
        source.linkChanged.connect(self._on_link)
        source.readingReady.connect(self._on_reading)
        source.localReady.connect(self._on_local)
        source.identityReady.connect(self._on_identity)
        source.openFailed.connect(self._on_open_failed)
        source.modelUnverified.connect(self._on_model_unverified)
        source.finished.connect(self._on_bridge_finished)

        self.rail.functionSelected.connect(self.bridge.setFunction)
        self.strip.streamToggled.connect(self.bridge.setStream)
        self.strip.singleRequested.connect(self.bridge.single)
        self.strip.configChanged.connect(self.bridge.setConfig)
        self.strip.triggerChanged.connect(self.bridge.setTrigger)
        self.strip.mathChanged.connect(self.bridge.setMath)

        self.stats_card.toggled.connect(
            lambda on: self.bridge.setMath.emit({"stats_on": on})
        )
        self.stats_card.clearRequested.connect(self.bridge.clearStats)
        self.limits_card.toggled.connect(
            lambda on: self.bridge.setMath.emit({"limit_on": on})
        )
        self.limits_card.boundsChanged.connect(self.bridge.setMath)

        self.capture.captureRequested.connect(self.bridge.captureScreen)
        self.capture.viewSelected.connect(
            lambda view: self.bridge.setDisplay.emit({"view": view})
        )
        self.capture.saveRequested.connect(self._save_frame)

        self.console_panel.send.connect(self.bridge.passthrough)

        self.log_panel.recordToggled.connect(self.bridge.setLog)
        self.log_panel.clearRequested.connect(self._clear_log)
        self.log_panel.exportRequested.connect(self._export_log)

        self.histogram_panel.refresh_button.clicked.connect(self.bridge.readHistogram)
        self.histogram_panel.clear_button.clicked.connect(self.bridge.clearHistogram)

        self.system_panel.beepRequested.connect(self.bridge.beep)
        self.system_panel.selftestRequested.connect(self._selftest)
        self.system_panel.lockRequested.connect(self.bridge.lock)
        self.system_panel.resetRequested.connect(self._reset)
        self.system_panel.refreshRequested.connect(self.bridge.readSystem)
        self.system_panel.displayTextRequested.connect(
            lambda text: self.bridge.setDisplay.emit({"text": text})
        )
        self.system_panel.displayOnRequested.connect(
            lambda on: self.bridge.setDisplay.emit({"on": on})
        )

        self.tabs.currentChanged.connect(self._tab_changed)

        # Kept as attributes so _set_phase can disable them: a shortcut is a
        # command to the instrument and must not fire during a handover.
        self.run_action = QAction("Run or stop", self)
        self.run_action.setShortcut(QKeySequence("Ctrl+R"))
        self.run_action.triggered.connect(
            lambda: self.bridge.setStream.emit(
                not bool(self.state and self.state.get("streaming"))
            )
        )
        self.addAction(self.run_action)

        self.refresh_action = QAction("Refresh", self)
        self.refresh_action.setShortcut(QKeySequence("F5"))
        self.refresh_action.triggered.connect(self.bridge.refreshState)
        self.addAction(self.refresh_action)

    # ------------------------------------------------------ the saved library

    @property
    def _connected_index(self) -> int:
        """Where the connected entry sits in the library *right now*.

        Derived, never stored: the connection dialog edits the library in
        place, so any index kept across it is a guess.  -1 means either
        nothing is connected or the connected entry has been removed from the
        library, and both correctly mean "no row is the connected one".
        """
        entry = self._connected_entry
        if entry is None:
            return -1
        return self.library.index_of(entry)

    def _active_entry(self) -> Optional[SavedInstrument]:
        """The library entry the current link belongs to, if any."""
        entry = self._connected_entry
        if entry is not None and self.library.index_of(entry) >= 0:
            return entry
        return None

    def _refresh_instrument_controls(self) -> None:
        """Rebuild the Instruments menu and the top-bar picker.

        Both are generated from the library rather than kept in step by hand,
        so a rename, an added meter or a change of connection cannot leave one
        of them showing something the other does not.
        """
        self._refreshing_controls = True
        try:
            live = self._phase == "connected"
            picker = self.instrument_picker
            picker.clear()
            for index, entry in enumerate(self.library.entries):
                mark = "● " if live and index == self._connected_index else ""
                # Just the name here: the address has its own field two
                # columns to the left, and repeating it only elides the name.
                picker.addItem(f"{mark}{entry.label or entry.address}", index)
                picker.setItemData(index, entry.display(), Qt.ToolTipRole)
            if not self.library.entries:
                picker.addItem("no saved instruments", None)
            chosen = (
                self._connected_index
                if self._connected_index >= 0
                else self.library.selected
            )
            if 0 <= chosen < picker.count():
                picker.setCurrentIndex(chosen)

            menu = self.instruments_menu
            menu.clear()
            for index, entry in enumerate(self.library.entries):
                action = QAction(entry.display(), menu)
                action.setCheckable(True)
                action.setChecked(live and index == self._connected_index)
                action.setStatusTip(
                    f"Connect to {entry.address} over {entry.transport}"
                )
                action.triggered.connect(
                    lambda _checked=False, i=index: self._menu_chose(i)
                )
                menu.addAction(action)
            if not self.library.entries:
                empty = QAction("No saved instruments", menu)
                empty.setEnabled(False)
                menu.addAction(empty)
            menu.addSeparator()
            manage = QAction("&Manage instruments…", menu)
            manage.triggered.connect(self.show_connect_dialog)
            menu.addAction(manage)
        finally:
            self._refreshing_controls = False

    def _menu_chose(self, index: int) -> None:
        """The Instruments menu picked an entry.

        Choosing the meter already connected used to perform a full teardown
        and rebuild — ``SYST:LOC``, socket closed, socket reopened, ranges
        re-enumerated — for no change at all, which is the link churn rule 3
        forbids.  The picker has always guarded this; the menu did not.
        """
        if self._phase == "connected" and index == self._connected_index:
            self.status.showMessage(
                f"Already connected to {self.library.entries[index].display()}.",
                4000,
            )
            self._refresh_instrument_controls()
            return
        self.connect_to_index(index)

    def _picker_changed(self, position: int) -> None:
        """The top-bar picker chose an entry.

        Offline it only changes what Connect will open.  While a link is up it
        switches instrument, which is the same one-click move the Instruments
        menu makes and the reason both exist.
        """
        if self._refreshing_controls or self._closing:
            return
        data = self.instrument_picker.itemData(position)
        if data is None:
            return
        index = int(data)
        if not 0 <= index < len(self.library):
            return
        self.library.selected = index
        self.library.save()
        if self._phase == "connected" and index != self._connected_index:
            self.connect_to_index(index)

    def show_connect_dialog(self) -> None:
        """Open the connection dialog; connect if the user pressed Connect."""
        if self._closing:
            return
        dialog = ConnectDialog(
            self.library,
            self,
            connected_index=(
                self._connected_index if self._phase == "connected" else -1
            ),
        )
        dialog.exec()
        self._refresh_instrument_controls()
        index = dialog.chosen_index()
        if index >= 0:
            self.connect_to_index(index)

    # ------------------------------------------------------ connection states

    def _set_phase(self, phase: str) -> None:
        """Say plainly what the link is doing, in one place.

        The top-bar button is the only connection control that is always
        visible, so it carries the state as well as the action: Connect,
        Connecting…, Disconnect, Disconnecting….  It is not clickable while an
        attempt or a handover is in flight, because both are sequences on the
        instrument that must not be interleaved.
        """
        self._phase = phase
        text, enabled, tip = {
            "offline": (
                "Connect",
                True,
                "Open a link to the selected instrument",
            ),
            "connecting": (
                "Connecting…",
                False,
                "Opening the link and identifying the instrument…",
            ),
            "connected": (
                "Disconnect",
                True,
                "Stop the run, restore the trigger setup, return the "
                "instrument to local with SYST:LOC and close the link",
            ),
            "disconnecting": (
                "Disconnecting…",
                False,
                "Restoring the trigger setup and handing the instrument back…",
            ),
        }[phase]
        self.link_button.setText(text)
        self.link_button.setEnabled(enabled)
        self.link_button.setToolTip(tip)
        self.link_button.setProperty("phase", phase)
        self.link_button.style().unpolish(self.link_button)
        self.link_button.style().polish(self.link_button)

        busy = phase in ("connecting", "disconnecting")
        self.connect_action.setEnabled(not busy)
        self.disconnect_action.setEnabled(phase == "connected")
        self.instruments_menu.setEnabled(not busy)
        self.instrument_picker.setEnabled(not busy)
        # The keyboard shortcuts are commands to the instrument like any
        # other, so they follow the same rule as the buttons that issue them:
        # pressed during a handover they would be delivered to a worker on its
        # way out and dropped with no feedback at all.
        for action in (self.run_action, self.refresh_action):
            action.setEnabled(phase == "connected")

    def _link_button_pressed(self) -> None:
        if self._phase == "connected":
            self.disconnect_instrument()
            return
        if self._phase != "offline":
            return
        index = self.library.selected
        if 0 <= index < len(self.library) and self.library.entries[index].address:
            self.connect_to_index(index)
        else:
            self.show_connect_dialog()

    def connect_to_index(self, index: int) -> None:
        """Connect to library entry *index*, handing back whatever is held.

        IO-DISCIPLINE.md rule 2 applies in full to the instrument being left,
        and it is not shortcut for a switch: the old worker is asked to stop,
        which stops the run, stops the keepalive, restores that meter's
        trigger configuration, sends ``SYST:LOC`` as the last command on its
        link and closes the socket.  Only when its thread has finished is the
        next worker built — see :meth:`_on_bridge_finished` and
        :meth:`Bridge.connect_to`.
        """
        if self._closing:
            return
        if not 0 <= index < len(self.library):
            self._show_banner("That instrument is no longer in the saved list.")
            return
        if self._phase in ("connecting", "disconnecting"):
            self.status.showMessage(
                "Wait for the instrument currently being handed back to "
                "finish.",
                5000,
            )
            return
        entry = self.library.entries[index]
        problem = validate_address(entry.address)
        if problem:
            self._show_banner(f"{entry.display()}: {problem}")
            self.show_connect_dialog()
            return

        self.library.selected = index
        self.library.save()
        self._pending_index = index
        if self.bridge.is_running():
            self._set_phase("disconnecting")
            self.status.showMessage(
                f"Handing {self.host} back before connecting to "
                f"{entry.address}…"
            )
            # request_stop() answers False when a handover is *already* under
            # way — after a watchdog fired, or after a Disconnect the user
            # then followed with a Connect.  The old code fell through to
            # _start_pending here, which consumed the pending index and then
            # had Bridge.connect_to refuse (correctly, rule 3): the request
            # was silently dropped and the phase machine had to be re-driven
            # by hand.  Keeping the pending index and simply waiting means the
            # connection happens as soon as that worker's thread ends.
            self.bridge.request_stop()
            self._arm_handover_watchdog()
            return
        self._start_pending()

    def _arm_handover_watchdog(self) -> int:
        """Start the watchdog for the handover beginning now.

        Each handover arms a fresh 30 s ``singleShot`` and Qt gives no way to
        cancel it, so every one is stamped with a token and only the newest
        may act.  Without that, an earlier timer fired during a *later*,
        healthy handover: it cleared ``_pending_index``, turning a switch into
        a plain disconnect so the user never reached meter B; it flipped the
        phase while the real worker was still shutting down; and it warned
        that a correctly handed-back meter might still be in remote.
        Switching around a bench is exactly the workload that arms these
        repeatedly.
        """
        self._handover_token += 1
        token = self._handover_token
        QTimer.singleShot(
            SHUTDOWN_TIMEOUT_MS, lambda t=token: self._handover_timed_out(t)
        )
        return token

    def _link_options(self, entry: SavedInstrument) -> Tuple[str, bool, str]:
        """The transport and finite-count flag to open *entry* with, and why.

        The saved entry is the default; a command-line flag overrides it for
        this run only.  The third element names any override in force, because
        an override with no visible cause is exactly what made the old
        behaviour — which rewrote the saved entry and never mentioned it —
        impossible to explain at the bench.
        """
        transport, finite = entry.transport, entry.finite
        if entry is not self._session_entry:
            return transport, finite, ""
        notes: List[str] = []
        if self._session_transport and self._session_transport != transport:
            notes.append(
                f"--transport {self._session_transport} (saved: {transport})"
            )
            transport = self._session_transport
        if self._session_finite and not finite:
            notes.append("--finite-trigger-count")
            finite = True
        if not notes:
            return transport, finite, ""
        return (
            transport,
            finite,
            "this session only, from the command line: "
            + ", ".join(notes)
            + "; the saved entry is unchanged",
        )

    def _start_pending(self) -> None:
        """Build the link the user asked for; nothing else is running now."""
        index, self._pending_index = self._pending_index, -1
        if not 0 <= index < len(self.library):
            return
        entry = self.library.entries[index]
        transport, finite, override = self._link_options(entry)
        self._reset_for_new_link()
        self.host = entry.address
        self._connected_entry = entry
        self._link_was_up = False
        self._handback_failed = False
        self._set_phase("connecting")
        self.ident_fields["ip"].setText(entry.address)
        self.setWindowTitle(f"Bench Buddy — {entry.display()}")
        self.status.showMessage(
            f"Connecting to {entry.address} over {transport}…"
            + (f"  ({override})" if override else "")
        )
        started, reason = self.bridge.connect_to(
            entry.address, self.port, transport, finite
        )
        if not started:
            self._connected_entry = None
            self.host = ""
            self._set_phase("offline")
            self._show_banner(f"Connecting to {entry.address}: {reason}")
            self.status.showMessage("Not connected", 8000)
        self._refresh_instrument_controls()

    def disconnect_instrument(self) -> None:
        """Hand the instrument back and close the link, leaving the app up."""
        if self._closing:
            return
        if not self.bridge.is_running():
            self.status.showMessage("No instrument is connected.", 4000)
            self._go_offline("No instrument is connected.")
            return
        if self._phase in ("connecting", "disconnecting"):
            return
        self._pending_index = -1
        self._set_phase("disconnecting")
        self.status.showMessage(
            "Restoring the trigger setup and handing the instrument back…"
        )
        # Whether this call started the stop or found one already running, a
        # worker is alive and its thread's end is what settles the phase.
        self.bridge.request_stop()
        self._arm_handover_watchdog()

    def _reset_for_new_link(self) -> None:
        """Forget everything that belonged to the previous instrument.

        Everything means everything.  The System panel — identity, LAN,
        **calibration date and count**, self-test result — the statistics, the
        histogram and the limits verdict all used to survive a switch, so
        meter A's serial and cal date sat under meter B's title; and if B then
        failed to connect they stayed on screen while the application was
        offline.  In a metrology tool a stale calibration date beside a live
        reading is the one thing that must never happen.
        """
        self.state = None
        self._rate_events.clear()
        self.chart_panel.chart.clear()
        self.log_panel.clear()
        self.readout.clear_value()
        self.readout.set_live(False)
        self.live_dot.set_live(False)
        self.capture.clear_frame()
        self.system_panel.clear()
        self.stats_card.clear_values()
        self.histogram_panel.clear()
        self.limits_card.set_verdict(None)
        self.rate_label.setText("0.0 rdg/s")
        for key in self.ident_fields:
            self.ident_fields[key].setText("—")
        self.banner.setVisible(False)

    def _go_offline(self, message: str) -> None:
        """Settle the window into the disconnected state, honestly."""
        self._connected_entry = None
        self.host = ""
        self.state = None
        self._set_phase("offline")
        self._set_link_label(False, False)
        self.strip.set_enabled_for_link(False)
        self.capture.set_enabled_for_link(False)
        self.live_dot.set_live(False)
        self.readout.set_live(False)
        self.setWindowTitle("Bench Buddy")
        self.status.showMessage(message, 10000)
        if self._offline_banner:
            self._show_banner(self._offline_banner)
            self._offline_banner = ""
        self._refresh_instrument_controls()

    def _on_bridge_finished(self) -> None:
        """The worker thread has ended: the instrument has been handed back."""
        # This handover is over, so its watchdog — and every earlier one — has
        # nothing left to declare failed.
        self._handover_token += 1
        if self._closing:
            self._shutdown_finished()
            return
        if self._pending_index >= 0:
            self._start_pending()
            return
        message = self._offline_message or (
            "Disconnected — but the instrument was not confirmed handed back; "
            "see the message above"
            if self._handback_failed
            else "Disconnected — the trigger setup was restored and the "
            "instrument's front panel is free-running again"
        )
        reopen = self._reopen_dialog
        self._offline_message = ""
        self._reopen_dialog = False
        self._go_offline(message)
        if reopen:
            QTimer.singleShot(0, self.show_connect_dialog)

    def _handover_timed_out(self, token: int) -> None:
        """The worker for the handover stamped *token* would not stop in time.

        Nothing new is opened.  Building a second link while the first
        instrument's handover is unfinished is exactly what would leave that
        meter in remote and acquiring, so the user is told instead.

        A pending Connect is dropped here deliberately — the address is still
        selected, and pressing Connect again queues it behind the worker that
        is refusing to finish rather than opening a second link.
        """
        if token != self._handover_token:
            return  # an earlier handover's timer; its own finished long ago
        if self._closing or self._phase != "disconnecting":
            return
        self._pending_index = -1
        self._set_phase("offline")
        self._show_banner(
            f"The instrument worker did not finish within "
            f"{SHUTDOWN_TIMEOUT_MS / 1000:g} s, so no new link was opened. "
            f"That meter may still be in remote — press [Local] on its front "
            f"panel. Connect will wait for that worker rather than opening a "
            f"second link."
        )

    def _on_open_failed(self, detail: str) -> None:
        """The first attempt on this link failed; there is nothing to retry.

        The worker is torn down rather than left cycling against an address
        the user is in the middle of correcting, and the connection dialog is
        put back in front of them.
        """
        if self._closing:
            return
        address = self.host or "the instrument"
        self._offline_message = f"Could not connect to {address}"
        # Kept and re-shown once the teardown has settled.  The worker's last
        # gasps — a keepalive retry notice, a failed state read — arrive after
        # this and would otherwise be the last thing on screen, which tells
        # the user far less than the connection error itself.
        self._offline_banner = f"Could not connect to {address}: {detail}"
        self._reopen_dialog = True
        self._show_banner(self._offline_banner)
        self._pending_index = -1
        self._set_phase("disconnecting")
        if self.bridge.request_stop() or self.bridge.is_running():
            # Either this call started the teardown or one was already
            # running; either way the thread's end settles the phase, watched
            # by a stamped watchdog.
            self._arm_handover_watchdog()
        else:
            self._on_bridge_finished()

    def _on_identity(self, identity: Dict[str, Any]) -> None:
        """``*IDN?`` came back: name the entry after the real instrument.

        A bench with three meters saved as three IP addresses is unusable, so
        an entry that still carries the application's own label takes the
        model and serial the instrument reports.  A label the user typed is
        never touched — :attr:`SavedInstrument.auto_label` is cleared the
        moment they edit it.
        """
        self._link_was_up = True
        entry = self._active_entry()
        if entry is not None and entry.auto_label:
            name = label_from_identity(identity)
            if name and name != entry.label:
                entry.label = name
                self.library.save()
                self.status.showMessage(
                    f"Saved this instrument as “{name}”", 6000
                )
        if entry is not None:
            self.setWindowTitle(
                f"Bench Buddy — {entry.display()}"
            )
        self.ident_fields["serial"].setText(str(identity.get("serial") or "—"))
        self.ident_fields["firmware"].setText(
            str(identity.get("firmware") or "—")
        )
        self._refresh_instrument_controls()

    def _on_model_unverified(self, identity: Dict[str, Any]) -> None:
        """``*IDN?`` named a model this command set was never probed against.

        It warns; it never blocks.  Someone with a 34465A on the bench can go
        ahead at their own discretion — but they are told which model answered
        and what is unverified about it first.

        **Nothing beyond ``*CLS`` and ``*IDN?`` has been sent while this
        dialog is up, and nothing more is sent until it is answered.**  That
        used to be untrue twice over: the ~24 ``<p>:RANG? MIN|MAX`` range
        queries ran *before* the guard — unsupported queries on an unknown
        model, whose measured failure mode is a hung socket — and the idle
        keepalive, a plain thread no Qt gate reached, kept driving the meter
        with ``ABOR``, ``TRIG:SOUR IMM``, ``INIT`` and ``R?`` for as long as
        the dialog stood open.  Both now wait behind this decision:
        ``Dmm34461A.open`` stops at the identity, and the keepalive is started
        by ``Bridge.proceedUnverified`` afterwards.

        See app/models.py, which is the single place a future verified model
        would be added.
        """
        if self._closing:
            return
        model = str(identity.get("model") or "").strip()
        found = model or "an instrument that did not name its model"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Unverified instrument model")
        box.setText(f"This is {found}, not a {PRIMARY_MODEL}.")
        box.setInformativeText(unverified_warning(model))
        proceed = box.addButton("Use it anyway", QMessageBox.AcceptRole)
        box.addButton("Disconnect", QMessageBox.RejectRole)
        box.setDefaultButton(proceed)
        box.exec()
        if box.clickedButton() is proceed:
            self._show_banner(
                f"Connected to {found}. Every command this application sends "
                f"was verified against a {PRIMARY_MODEL} only, so functions, "
                f"ranges and the never-send list may be wrong for this "
                f"instrument.",
                "warn",
            )
            self.bridge.proceedUnverified.emit()
        else:
            self.disconnect_instrument()

    def _about(self) -> None:
        entry = self._active_entry()
        where = entry.display() if entry and self._phase == "connected" else "nothing"
        QMessageBox.about(
            self,
            "About Bench Buddy",
            f"<b>Bench Buddy</b><br>version {__version__}"
            f"<br><br>A control application for the Keysight {PRIMARY_MODEL} "
            f"bench digital multimeter, speaking SCPI over LAN."
            f"<br><br>Connected to: {where}."
            f"<br><br>The instrument is returned to local control with "
            f"SYST:LOC whenever this application disconnects, so its front "
            f"panel free-runs again."
        )

    # --------------------------------------------------------------- handlers

    def _on_state(self, state: Dict[str, Any]) -> None:
        previous = self.state
        self.state = state
        if previous is not None:
            was, now = _reading_kind(previous), _reading_kind(state)
            if was != now:
                # A *function* change was already handled here.  A scaling
                # change was not, and it is the same invalidation: the chart
                # relabels its axis the moment CALC:SCAL:STAT moves, so volt
                # samples sat in the ring buffer under a dB axis, autoscaled
                # together with the dB ones and reported in dB by the
                # crosshair; and the log table formats every historical row
                # with the *current* state, so a table of volts silently
                # became a table of dB.  Both buffers are dropped whenever the
                # quantity being displayed changes.
                self.chart_panel.chart.clear()
                self.log_panel.clear()
                self.readout.clear_value()
                self.status.showMessage(
                    f"{_reading_change_text(was, now)} — the chart and the log "
                    f"view were cleared, because the readings before and after "
                    f"are not the same quantity",
                    6000,
                )

        self.readout.set_state(state)
        self.readout.set_flags(self._flags(state))
        self.readout.set_live(bool(state.get("streaming")))
        self.strip.apply_state(state)
        self.chart_panel.apply_state(state)
        self.histogram_panel.apply_state(state)
        self.log_panel.apply_state(state)
        self.capture.apply_state(state)
        self.stats_card.apply_state(state)
        self.limits_card.apply_state(state)
        self.system_panel.apply_state(state)
        self.rail.set_function(str(state.get("func") or ""))

        self.live_dot.set_live(bool(state.get("streaming")))
        connected = bool(state.get("connected"))
        self.strip.set_enabled_for_link(connected)
        self.capture.set_enabled_for_link(connected)
        self._set_link_label(connected, bool(state.get("streaming")))

        if state.get("memory_overflow"):
            self._show_banner(
                "The instrument's reading memory overflowed and samples were "
                "dropped. Reduce NPLC or stop other polling."
            )
        if state.get("overload"):
            self.readout.set_value(None, True)

    def _flags(self, state: Dict[str, Any]) -> List[Tuple[str, str]]:
        math_state = state.get("math") or {}
        flags: List[Tuple[str, str]] = []
        if state.get("streaming"):
            flags.append(("Running", "run"))
        if state.get("range_auto"):
            flags.append(("Auto", "on"))
        if math_state.get("null_on"):
            flags.append(("Null", "on"))
        if math_state.get("scale_on"):
            flags.append((str(math_state.get("scale_func") or "Scale"), "on"))
        if math_state.get("stats_on"):
            flags.append(("Stats", "on"))
        if math_state.get("limit_on"):
            flags.append(("Limits", "warn"))
        if math_state.get("hist_on"):
            flags.append(("Hist", "on"))
        if state.get("logging"):
            flags.append(("Log", "warn"))
        if state.get("log_overflow"):
            flags.append(("Log full", "fail"))
        return flags

    def _on_data(self, message: Dict[str, Any]) -> None:
        values: List[Optional[float]] = list(message.get("v") or [])
        if not values:
            return
        timestamp = float(message.get("t") or time.time())

        now = time.monotonic()
        self._rate_events.append((now, len(values)))
        cutoff = now - 1.0
        while self._rate_events and self._rate_events[0][0] < cutoff:
            self._rate_events.popleft()
        rate = float(sum(count for _, count in self._rate_events))

        last = values[-1]
        self.readout.set_value(last, last is None)
        self.chart_panel.chart.push_batch(timestamp, values, rate)
        if self.state and self.state.get("logging"):
            self.log_panel.buffer(timestamp, values, rate)

    def _on_stats(self, stats: Dict[str, Any]) -> None:
        self.stats_card.set_stats(stats, self.state)

    def _on_limit(self, message: Dict[str, Any]) -> None:
        self.limits_card.set_verdict(str(message.get("status") or ""))

    def _on_single(self, result: Dict[str, Any]) -> None:
        value = result.get("value")
        overload = bool(result.get("overload"))
        self.readout.set_value(value, overload)
        self.readout.update()
        text = "OVLD" if overload else fmt_state(value, self.state, 7)
        self.status.showMessage(f"READ? returned {text}", 6000)

    def _on_error(self, text: str) -> None:
        if not text:
            return
        if "capturing the instrument screen" in text:
            self.capture.capture_failed()
        self._show_banner(text)
        self.console_panel.note(text)

    def _on_notice(self, text: str) -> None:
        self.status.showMessage(text, 5000)

    def _on_system(self, info: Dict[str, Any]) -> None:
        self.system_panel.set_system(info)
        self.ident_fields["serial"].setText(str(info.get("serial") or "—"))
        self.ident_fields["firmware"].setText(str(info.get("firmware") or "—"))
        lan = info.get("lan") or {}
        self.ident_fields["ip"].setText(str(lan.get("ip") or self.host))
        entry = self._active_entry()
        name = entry.display() if entry is not None else self.host
        self.setWindowTitle(f"Bench Buddy — {name}")

    def _on_histogram(self, data: Dict[str, Any]) -> None:
        self.histogram_panel.set_data(data, self.state)

    def _on_frame(self, png: bytes, stamp: float) -> None:
        self.capture.set_frame(png, stamp)

    def _on_reading(self, message: Dict[str, Any]) -> None:
        """An idle heartbeat reading (IO-DISCIPLINE.md rule 1).

        It keeps the readout — and the instrument's own front panel — live
        while nothing is streaming.  It is deliberately not pushed into the
        chart or the log: those belong to a run, and an idle keepalive is not
        one.
        """
        if self.state and self.state.get("streaming"):
            return
        value = message.get("v")
        self.readout.set_value(value, bool(message.get("ovld")))

    def _on_local(self, result: Dict[str, Any]) -> None:
        if not self._link_was_up:
            # Nothing was ever taken from this instrument, so there is nothing
            # to report about handing it back.
            return
        detail = str(result.get("detail") or "")
        self._handback_failed = not result.get("local")
        if result.get("local"):
            message = (
                "Instrument returned to local with SYST:LOC — the front panel "
                "is free-running again"
            )
            self.status.showMessage(message, 8000)
            if detail:
                # SYST:LOC went out and the panel is back, but the instrument
                # had errors queued beforehand and the user should see them.
                self._show_banner(
                    "Returned to local, but the instrument's error queue was "
                    f"not clean beforehand: {detail}"
                )
        else:
            self._show_banner(
                "The instrument was not returned to local: "
                f"{detail or 'the instrument gave no reason'}. "
                "Press [Local] on the instrument's front panel instead."
            )

    def _on_export(self, path: str, written: int) -> None:
        self.status.showMessage(f"Wrote {written:,} bytes to {path}", 8000)

    def _on_link(self, connected: bool, detail: str) -> None:
        self._set_link_label(connected, bool(self.state and self.state.get("streaming")))
        if connected:
            self.banner.setVisible(False)
            if self._phase == "connecting":
                self._set_phase("connected")
                self._refresh_instrument_controls()
            self.status.showMessage(f"Connected to {self.host}", 4000)
        elif detail:
            # A link that was up and dropped: the worker is still there and
            # its supervisor will re-open it, so the phase stays "connected"
            # and Disconnect remains the way out.  A first attempt that never
            # succeeded arrives at _on_open_failed instead.
            self._show_banner(detail)

    def _set_link_label(self, connected: bool, streaming: bool) -> None:
        if not connected:
            self.link_label.setText("Offline")
            self.link_label.setStyleSheet(f"color: {theme.FAIL};")
        elif streaming:
            self.link_label.setText("Running")
            self.link_label.setStyleSheet(f"color: {theme.SIGNAL};")
        else:
            self.link_label.setText("Connected")
            self.link_label.setStyleSheet(f"color: {theme.OK};")
        self.local_button.setEnabled(connected)
        self._set_keepalive_label(connected, streaming)

    def _set_keepalive_label(self, connected: bool, streaming: bool) -> None:
        """Say plainly whether the instrument is still measuring."""
        beat = (self.state or {}).get("heartbeat") or {}
        if not connected or (self.state or {}).get("local"):
            # Either there is no link, or the instrument has deliberately been
            # handed back: in both cases the panel is free-running and nothing
            # here is holding it, which is the good outcome rather than a fault.
            self.keepalive_label.setText("front panel is on its own")
            self.keepalive_label.setToolTip(
                "The application is not driving the instrument. Use Run or "
                "Single to take it back."
            )
            self.keepalive_label.setStyleSheet(f"color: {theme.DIM};")
        elif streaming:
            self.keepalive_label.setText("panel live · run measuring")
            self.keepalive_label.setToolTip(self._deadman_tooltip())
            self.keepalive_label.setStyleSheet(f"color: {theme.DIM};")
        elif beat.get("beating"):
            self.keepalive_label.setText(
                f"panel live · {beat.get('hz', 0):g} Hz keepalive"
            )
            self.keepalive_label.setToolTip(self._deadman_tooltip())
            self.keepalive_label.setStyleSheet(f"color: {theme.DIM};")
        else:
            reason = str(beat.get("reason") or "the keepalive is not running")
            self.keepalive_label.setText("panel holding — hover for why")
            self.keepalive_label.setToolTip(reason)
            self.keepalive_label.setStyleSheet(f"color: {theme.WARN};")

    def _deadman_tooltip(self) -> str:
        """Say which thing would stop the meter if this application died.

        The two cases are genuinely different for the person at the bench, so
        the tooltip states which one is in force rather than implying the
        protection is unconditional.
        """
        state = self.state or {}
        beat = state.get("heartbeat") or {}
        transport = str(state.get("transport") or "not connected")
        lines = [f"Link: {transport}."]
        if state.get("crash_safe"):
            lines.append(
                "If this application is killed, the instrument ends the "
                "acquisition and returns its front panel to free-running "
                "within about two seconds, by itself."
            )
            if beat.get("forced_finite"):
                lines.append(
                    "The finite renewed trigger count is forced on as well "
                    "(--finite-trigger-count), which blanks the reading for "
                    "one integration period every few seconds."
                )
        else:
            lines.append(
                "This link has no session semantics, so the instrument cannot "
                "tell a dead application from a quiet one. The keepalive's "
                "finite renewed trigger count is the only thing that would "
                "stop it, and it does not cover a run."
            )
            note = state.get("transport_note")
            if note:
                lines.append(str(note))
        return "\n\n".join(lines)

    def _show_banner(self, text: str, level: str = "fail") -> None:
        """The one place bad news appears above the readout.

        *level* is ``"fail"`` for something that went wrong and ``"warn"`` for
        something the user needs to know but that is not a failure — an
        unverified instrument model, a settings file that would not parse.
        """
        colour = theme.WARN if level == "warn" else theme.FAIL
        ground = "#2A2113" if level == "warn" else "#2A1A18"
        self.banner.setStyleSheet(
            f"QFrame#Panel {{ background: {ground}; border: 1px solid {colour}; }}"
        )
        self.banner_text.setStyleSheet(f"color: {colour};")
        self.banner_text.setText(text)
        self.banner.setVisible(True)

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.histogram_panel:
            self.bridge.readHistogram.emit()

    # ---------------------------------------------------------------- actions

    def _clear_log(self) -> None:
        self.log_panel.clear()
        self.bridge.clearLog.emit()

    def _export_log(self) -> None:
        default = os.path.join(
            os.path.expanduser("~"),
            time.strftime("34461A-log-%Y%m%d-%H%M%S.csv"),
        )
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export the data log", default, "CSV files (*.csv)"
        )
        if path:
            self.status.showMessage(f"Writing {path}…")
            self.bridge.exportLog.emit(path)

    def _save_frame(self) -> None:
        frame = self.capture.latest_frame()
        if frame is None:
            self.status.showMessage(
                "No frame has been captured yet — press Capture screen first", 6000
            )
            return
        default = os.path.join(
            os.path.expanduser("~"),
            time.strftime("34461A-screen-%Y%m%d-%H%M%S.png"),
        )
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save the instrument screen", default, "PNG images (*.png)"
        )
        if path:
            self.bridge.saveFrame.emit({"path": path, "data": frame})

    def _return_to_local(self) -> None:
        """Rule 6's explicit action, and the same path shutdown takes."""
        answer = QMessageBox.question(
            self,
            "Return the instrument to local?",
            "The trigger setup is restored and SYST:LOC is sent, so the front "
            "panel starts free-running again.\n\n"
            "The idle keepalive stops, because resuming it would put the "
            "instrument straight back into remote. Use Run, Single or "
            "Reconnect when you want the app driving it again.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self.bridge.returnToLocal.emit()

    def _selftest(self) -> None:
        answer = QMessageBox.question(
            self,
            "Run the self test?",
            "*TST? takes up to 20 seconds and aborts any acquisition in "
            "progress. Run it now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.status.showMessage("Running *TST? — this takes up to 20 s…")
            self.bridge.selftest.emit()

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset the instrument?",
            "*RST returns the 34461A to its power-on setup and discards the "
            "current configuration. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.bridge.reset.emit()

    # ------------------------------------------------------------------- tick

    def _tick(self) -> None:
        rate = 0.0
        if self._rate_events:
            cutoff = time.monotonic() - 1.0
            while self._rate_events and self._rate_events[0][0] < cutoff:
                self._rate_events.popleft()
            rate = float(sum(count for _, count in self._rate_events))
        self.rate_label.setText(f"{rate:.1f} rdg/s")

        self.readout.update()
        self.chart_panel.chart.tick()
        self.log_panel.tick()
        self.capture.tick()
        self.live_dot.tick()

    # --------------------------------------------------------------- lifecycle

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = event.size().width()
        self.right_rail.setVisible(width >= RAIL_COLLAPSE_PX)
        self.rail.setVisible(width >= FUNCTION_RAIL_COLLAPSE_PX)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Hand the instrument back before the window goes.

        The shutdown is watched, not waited for.  Blocking the GUI thread on
        ``bridge.stop()`` froze the whole interface for as long as the handover
        took — up to its 30 s timeout if a worker was wedged in a socket read —
        and the ``processEvents()`` that used to precede it was worse than the
        freeze: a nested event loop, dispatching whatever the user had already
        clicked, *after* ``_closing`` was set and while the instrument was
        being put back.  Here the window simply stops accepting input and waits
        for the worker's thread to finish.
        """
        if self._shutdown_done:
            event.accept()
            return
        if self._closing:
            # Already handing back; ignore the repeat rather than tearing the
            # process down underneath the worker.
            event.ignore()
            return
        self._closing = True
        self._pending_index = -1
        self._timer.stop()
        self.setEnabled(False)
        self.status.showMessage(
            "Restoring the trigger setup and handing the instrument back…"
        )
        # ``Bridge.finished`` is already wired to _on_bridge_finished, which
        # forwards to _shutdown_finished once _closing is set.  It has to be
        # the bridge's signal rather than the thread's: the thread object is
        # replaced every time the user connects to a different instrument.
        QTimer.singleShot(SHUTDOWN_TIMEOUT_MS, self._shutdown_timed_out)
        if self.bridge.is_running():
            # request_stop() answers False when a handover is already under
            # way — closing during a Disconnect, or mid-switch between two
            # meters.  The test has to be whether a worker still exists, not
            # whether this call started the stop: treating "already stopping"
            # as "nothing to stop" would close the window out from under an
            # instrument that had not finished being handed back.
            self.bridge.request_stop()
        else:
            self._shutdown_finished()
        event.ignore()

    def _shutdown_finished(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._shutdown_ok = True
        self._finish_quit()

    def _finish_quit(self) -> None:
        """Close the window and end the process.

        ``close()`` alone is not enough. The handover is asynchronous, so the
        first close request is always refused with ``event.ignore()`` while the
        instrument is put back — and on macOS refusing that event *cancels the
        application quit* that Cmd+Q started. The window would then close when
        the handover finished, but the process stayed alive, so quitting
        appeared to need two attempts.

        Relying on Qt's quit-on-last-window-closed instead is not dependable
        here either: any surviving top-level widget, such as the connection
        dialog, keeps the application running. Ending it explicitly is the only
        behaviour that is the same on all three platforms.
        """
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _shutdown_timed_out(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._shutdown_ok = False
        sys.stderr.write(
            "the instrument worker did not stop within "
            f"{SHUTDOWN_TIMEOUT_MS / 1000:g} s; the instrument may still be in "
            "remote — press [Local] on its front panel\n"
        )
        self._finish_quit()


def _env_flag(name: str) -> bool:
    """A boolean environment variable, read the way people write them.

    ``DMM_FINITE_TRIGGER_COUNT=false`` used to read as *true*: anything but
    ``""`` or ``"0"`` was taken as set, so the word that most plainly means
    "off" switched the option on — and, before the flags became
    session-scoped, wrote it permanently into the saved entry.  Unrecognised
    values stay true, because an explicitly set variable is an intent to
    enable, but every ordinary way of writing "no" is honoured.
    """
    raw = (os.environ.get(name) or "").strip().lower()
    return raw not in ("", "0", "false", "no", "off", "n", "f")


def build_application(argv: Optional[List[str]] = None) -> Tuple[QApplication, MainWindow]:
    """Parse the command line, load the saved library and build the window.

    Startup precedence, in order:

    1. ``--instrument`` on the command line, or ``DMM_HOST`` in the
       environment.  Either wins over anything saved, and the address is added
       to the saved library so it also appears in the Instruments menu.  The
       *address* is all that is saved: ``--transport`` and
       ``--finite-trigger-count`` apply to this run only.
    2. otherwise the entry that was connected last time.
    3. otherwise nothing is opened and the connection dialog is shown.  There
       is deliberately no built-in default address any more: an installed copy
       silently dialling whatever host the developer happened to use is the
       bug this replaces.
    """
    parser = argparse.ArgumentParser(
        prog="bench-buddy",
        description=(
            f"Bench Buddy — control application for the Keysight "
            f"{PRIMARY_MODEL}"
        ),
    )
    parser.add_argument(
        "--instrument",
        default=None,
        help=(
            "instrument host or IP.  Overrides the saved selection; with "
            "neither given, the connection dialog is shown."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"SCPI raw socket port (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORT_KEYS,
        default=None,
        help=(
            "session type (default auto), for this run only — the saved entry "
            "is not changed.  VXI-11 is preferred because the instrument stops "
            "acquiring by itself if this process is killed; the raw socket has "
            "no session semantics and cannot do that."
        ),
    )
    parser.add_argument(
        "--finite-trigger-count",
        action="store_true",
        default=False,
        help=(
            "keep the idle keepalive's finite renewed trigger count even on a "
            "transport that already stops the acquisition when this process "
            "dies.  Belt and braces: it costs one blanked sample in twelve on "
            "the front panel.  It is used automatically whenever the link is "
            "not crash-safe, so this is only needed to force it on.  Applies "
            "to this run only; use the connection dialog's checkbox to make it "
            "part of a saved instrument."
        ),
    )
    arguments = parser.parse_args(argv if argv is not None else sys.argv[1:])

    host = (arguments.instrument or os.environ.get("DMM_HOST") or "").strip()
    transport = arguments.transport or os.environ.get("DMM_TRANSPORT") or ""
    if transport not in TRANSPORT_KEYS:
        transport = ""
    finite = arguments.finite_trigger_count or _env_flag(
        "DMM_FINITE_TRIGGER_COUNT"
    )

    # Before anything opens a link: if this process dies without reaching
    # closeEvent, these handlers still try to hand the instrument back.  They
    # are defence in depth — on a VXI-11 link the instrument's own teardown is
    # what actually protects it from a hard kill, and on the raw-socket
    # fallback the acquisition's finite trigger count is.
    install_safety_net()

    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("Bench Buddy")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("bench-buddy")
    theme.apply(application)

    # QSettings picks up the organisation and application names set above, so
    # it has to be built after them.
    library = InstrumentLibrary(QSettings())
    library.load()

    index = -1
    if host:
        index = library.find(host)
        if index < 0:
            index = library.add(
                # Defaults, not the session flags: a brand-new entry created
                # from ``--instrument`` must not be born carrying this run's
                # overrides, or the very first ``--finite-trigger-count`` run
                # would still write that meter's behaviour into the library
                # for ever.  The flags reach the window as session overrides
                # against this entry instead (see _link_options).
                SavedInstrument(address=host, transport="auto", finite=False)
            )
        library.selected = index
    elif (
        0 <= library.selected < len(library)
        and library.entries[library.selected].address
    ):
        index = library.selected

    if host:
        # Only the *address* is persisted, because a saved library the user
        # can see and edit is the point of it.  The two flags are not: they
        # describe this run of the application, and writing them into the
        # entry made one ``--finite-trigger-count`` run change that meter's
        # behaviour for ever, with nothing in the interface saying why.  They
        # are handed to the window as session overrides instead.
        library.save()

    window = MainWindow(
        library,
        arguments.port,
        index,
        session_transport=transport,
        session_finite=finite,
    )
    return application, window


def main(argv: Optional[List[str]] = None) -> int:
    application, window = build_application(argv)
    window.show()
    return int(application.exec())
