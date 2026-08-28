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

"""The System tab: identity, LAN, calibration, status and the panel actions.

Every field here is read from the instrument.  ``CAL:DATE?`` and ``CAL:STR?``
disagree by a day on this unit, so both are shown separately rather than
reconciled into one date that would be wrong either way.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme


def _clock(info: Dict[str, Any]) -> str:
    """SYST:DATE? and SYST:TIME? as one readable stamp.

    ``SYST:TIME?`` answers ``+14,+52,+16.055`` — comma separated fields with
    explicit signs, not a clock time.
    """
    date = str(info.get("date") or "").strip()
    raw = str(info.get("time") or "").strip()
    if not raw:
        return date
    parts = [piece.strip().lstrip("+") for piece in raw.split(",") if piece.strip()]
    if len(parts) >= 3:
        try:
            time_text = "%02d:%02d:%06.3f" % (
                int(float(parts[0])),
                int(float(parts[1])),
                float(parts[2]),
            )
        except ValueError:
            time_text = raw
    else:
        time_text = raw
    return f"{date} {time_text}".strip()


def _card(title: str) -> tuple:
    frame = QFrame()
    frame.setObjectName("Panel")
    box = QVBoxLayout(frame)
    box.setContentsMargins(12, 10, 12, 12)
    box.setSpacing(8)
    heading = QLabel(title)
    heading.setObjectName("SectionLabel")
    heading.setFont(theme.label())
    box.addWidget(heading)
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(14)
    grid.setVerticalSpacing(4)
    grid.setColumnStretch(1, 1)
    box.addLayout(grid)
    # Cards sit side by side and stretch to the tallest of the row; without
    # this the spare height is shared out between the heading and the rows.
    box.addStretch(1)
    return frame, grid


class FieldGrid:
    """A card of key/value rows, filled from a dict."""

    def __init__(self, title: str, keys: List[tuple]) -> None:
        self.frame, self._grid = _card(title)
        self._values: Dict[str, QLabel] = {}
        for row, (key, label) in enumerate(keys):
            name = QLabel(label)
            name.setObjectName("IdentKey")
            name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            value = QLabel("—")
            value.setObjectName("Ident")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            self._grid.addWidget(name, row, 0)
            self._grid.addWidget(value, row, 1)
            self._values[key] = value

    def set(self, key: str, text: str) -> None:
        label = self._values.get(key)
        if label is not None:
            label.setText(text if text else "—")

    def clear(self) -> None:
        """Back to em dashes — nothing here belongs to another instrument."""
        for label in self._values.values():
            label.setText("—")


class SystemPanel(QWidget):
    """Identity, LAN, calibration, status, and the instrument-level actions."""

    beepRequested = Signal()
    selftestRequested = Signal()
    lockRequested = Signal(bool)
    resetRequested = Signal()
    refreshRequested = Signal()
    displayTextRequested = Signal(str)
    displayOnRequested = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.identity = FieldGrid(
            "IDENTITY   ·   *IDN?",
            [
                ("model", "MODEL"),
                ("serial", "SERIAL"),
                ("firmware", "FIRMWARE"),
                ("options", "OPTIONS"),
                ("terminals", "TERMINALS"),
                ("lfr", "LINE FREQ"),
                ("uptime", "UPTIME"),
                ("datetime", "CLOCK"),
            ],
        )
        self.lan = FieldGrid(
            "LAN   ·   SYST:COMM:LAN",
            [
                ("hostname", "HOSTNAME"),
                ("ip", "IP"),
                ("mac", "MAC"),
                ("dhcp", "DHCP"),
                ("subnet", "SUBNET"),
                ("gateway", "GATEWAY"),
                ("dns", "DNS"),
                ("domain", "DOMAIN"),
                ("telnet", "TELNET WELCOME"),
                ("lxi", "LXI IDENTIFY"),
            ],
        )
        self.calibration = FieldGrid(
            "CALIBRATION   ·   CAL:DATE? and CAL:STR? disagree by a day on this unit",
            [
                ("count", "CAL COUNT"),
                ("date", "CAL:DATE?"),
                ("string", "CAL:STR?"),
                ("secure", "SYST:SEC:COUN?"),
            ],
        )
        self.status = FieldGrid(
            "STATUS",
            [
                ("questionable", "STAT:QUES:COND?"),
                ("operation", "STAT:OPER:COND?"),
                ("esr", "*ESR?"),
                ("stb", "*STB?"),
                ("beeper", "SYST:BEEP:STAT?"),
                ("click", "SYST:CLIC:STAT?"),
                ("lock", "SYST:LOCK:OWN?"),
                ("errors", "SYST:ERR? QUEUE"),
            ],
        )

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.identity.frame, 1)
        row.addWidget(self.lan.frame, 1)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(self.calibration.frame, 1)
        row2.addWidget(self.status.frame, 1)
        layout.addLayout(row2)

        actions, _ = _card("ACTIONS")
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        for text, node, signal in (
            ("Beep", "SYST:BEEP", self.beepRequested),
            ("Self test", "*TST?", self.selftestRequested),
            ("Lock front panel", "SYST:LOCK:REQ?", None),
            ("Release lock", "SYST:LOCK:REL", None),
            ("Reset", "*RST", self.resetRequested),
            ("Refresh", "system read-back", self.refreshRequested),
        ):
            button = QPushButton(text)
            button.setToolTip(node)
            if signal is not None:
                button.clicked.connect(signal)
            elif text.startswith("Lock"):
                button.clicked.connect(lambda: self.lockRequested.emit(True))
            else:
                button.clicked.connect(lambda: self.lockRequested.emit(False))
            actions_row.addWidget(button)
        actions_row.addStretch(1)
        actions.layout().addLayout(actions_row)

        text_row = QHBoxLayout()
        text_row.setSpacing(8)
        text_label = QLabel("DISPLAY TEXT")
        text_label.setObjectName("SectionLabel")
        text_label.setFont(theme.label())
        text_row.addWidget(text_label)
        self.text_input = QLineEdit()
        self.text_input.setMaxLength(40)
        self.text_input.setPlaceholderText("up to 40 characters, no quote marks")
        self.text_input.returnPressed.connect(self._send_text)
        text_row.addWidget(self.text_input, 1)
        send_text = QPushButton("Show")
        send_text.setToolTip('DISP:TEXT "…"')
        send_text.clicked.connect(self._send_text)
        text_row.addWidget(send_text)
        clear_text = QPushButton("Clear")
        clear_text.setToolTip("DISP:TEXT:CLE")
        clear_text.clicked.connect(self._clear_text)
        text_row.addWidget(clear_text)
        self.display_button = QPushButton("Display on")
        self.display_button.setObjectName("Toggle")
        self.display_button.setCheckable(True)
        self.display_button.setToolTip("DISP")
        self.display_button.toggled.connect(self._display_toggled)
        text_row.addWidget(self.display_button)
        actions.layout().addLayout(text_row)
        layout.addWidget(actions)

        self.selftest_result = QLabel("")
        self.selftest_result.setObjectName("Value")
        self.selftest_result.setWordWrap(True)
        layout.addWidget(self.selftest_result)

        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self._syncing = False

    # --------------------------------------------------------------- actions

    def _send_text(self) -> None:
        self.displayTextRequested.emit(self.text_input.text())

    def _clear_text(self) -> None:
        self.text_input.clear()
        self.displayTextRequested.emit("")

    def _display_toggled(self, on: bool) -> None:
        if self._syncing:
            return
        self.displayOnRequested.emit(on)

    # ----------------------------------------------------------------- input

    def set_system(self, info: Dict[str, Any]) -> None:
        self.identity.set("model", str(info.get("model", "")))
        self.identity.set("serial", str(info.get("serial", "")))
        self.identity.set("firmware", str(info.get("firmware", "")))
        self.identity.set("options", str(info.get("options", "")))
        self.identity.set("terminals", str(info.get("terminals", "")))
        lfr = info.get("lfr")
        self.identity.set("lfr", f"{lfr:g} Hz" if isinstance(lfr, (int, float)) else "")
        uptime = info.get("uptime") or {}
        self.identity.set("uptime", str(uptime.get("text", "")))
        self.identity.set("datetime", _clock(info))

        lan = info.get("lan") or {}
        self.lan.set("hostname", str(lan.get("hostname", "")))
        self.lan.set("ip", str(lan.get("ip", "")))
        self.lan.set("mac", str(lan.get("mac", "")))
        self.lan.set("dhcp", "on" if lan.get("dhcp") else "off")
        self.lan.set("subnet", str(lan.get("subnet", "")))
        self.lan.set("gateway", str(lan.get("gateway", "")))
        self.lan.set("dns", str(lan.get("dns", "")))
        self.lan.set("domain", str(lan.get("domain", "")))
        self.lan.set("telnet", str(lan.get("telnet_welcome", "")))
        self.lan.set("lxi", "identifying" if lan.get("lxi_identify") else "off")

        cal = info.get("cal") or {}
        self.calibration.set("count", str(cal.get("count", "")))
        self.calibration.set("date", str(cal.get("date", "")))
        self.calibration.set("string", str(cal.get("string", "")))
        self.calibration.set("secure", str(info.get("secure_count", "")))

        self.status.set("questionable", str(info.get("questionable", "")))
        self.status.set("operation", str(info.get("operation", "")))
        self.status.set("esr", str(info.get("esr", "")))
        self.status.set("stb", str(info.get("stb", "")))
        self.status.set("beeper", "on" if info.get("beeper") else "off")
        self.status.set("click", "on" if info.get("click") else "off")
        self.status.set("lock", str(info.get("lock_owner", "")))
        errors = info.get("errors") or []
        self.status.set("errors", "; ".join(errors) if errors else "empty")

    def clear(self) -> None:
        """Drop every figure read from an instrument (identity, LAN, cal…).

        Called when the link moves to another meter.  A calibration date or a
        serial from the previous instrument under the new one's title would be
        indistinguishable from a live reading of the new one.
        """
        for grid in (self.identity, self.lan, self.calibration, self.status):
            grid.clear()
        self.selftest_result.setText("")
        self.selftest_result.setStyleSheet("")
        self._syncing = True
        self.text_input.clear()
        self.display_button.setChecked(False)
        self.display_button.setText("Display off")
        self._syncing = False

    def set_selftest(self, result: Dict[str, Any]) -> None:
        passed = bool(result.get("passed"))
        errors = result.get("errors") or []
        text = f"*TST? returned {result.get('result', '')} — " + (
            "the instrument passed its self test."
            if passed
            else "the instrument reported a failure."
        )
        if errors:
            text += "  Error queue: " + "; ".join(errors)
        self.selftest_result.setText(text)
        self.selftest_result.setStyleSheet(
            f"color: {theme.OK};" if passed else f"color: {theme.FAIL};"
        )

    def apply_state(self, state: Dict[str, Any]) -> None:
        display = state.get("display") or {}
        on = display.get("on")
        self._syncing = True
        self.display_button.setChecked(bool(on))
        self.display_button.setText("Display on" if on else "Display off")
        self._syncing = False
        text = display.get("text")
        if text is not None and not self.text_input.hasFocus():
            if self.text_input.text() != text:
                self.text_input.setText(str(text))
