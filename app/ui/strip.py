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

"""The function rail and the softkey control strip.

The rail is the rotary switch: the twelve functions with the mnemonics on the
instrument's own dial.  The strip beneath the readout mirrors the 34461A's
softkey row — a control appears only when the state object carries the field it
writes, so selecting Continuity leaves no dead Range key behind.

Every control carries the SCPI node it writes as a caption, regenerated per
function: RANGE reads ``VOLT:DC:RANG`` on DC volts and ``FRES:RANG`` on 4-wire.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import specs
from . import theme
from .units import fmt_range, is_num, node_for, trim_float

FUNCTIONS: Tuple[Tuple[str, str, str, bool], ...] = (
    # key, mnemonic, label, whether a rule follows it on the rail
    ("VOLT:DC", "DCV", "DC Voltage", False),
    ("VOLT:AC", "ACV", "AC Voltage", False),
    ("CURR:DC", "DCI", "DC Current", False),
    ("CURR:AC", "ACI", "AC Current", True),
    ("RES", "2W", "2-Wire Resistance", False),
    ("FRES", "4W", "4-Wire Resistance", True),
    ("FREQ", "FREQ", "Frequency", False),
    ("PER", "PER", "Period", True),
    ("CAP", "CAP", "Capacitance", True),
    ("CONT", "CONT", "Continuity", False),
    ("DIOD", "DIODE", "Diode", True),
    ("TEMP", "TEMP", "Temperature", False),
)


class FunctionRail(QFrame):
    """The rotary-switch equivalent."""

    functionSelected = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Rail")
        self.setFixedWidth(148)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(2)

        heading = QLabel("FUNCTION")
        heading.setObjectName("SectionLabel")
        heading.setFont(theme.label())
        layout.addWidget(heading)

        node = QLabel("CONF:<func>")
        node.setObjectName("Caption")
        layout.addWidget(node)
        layout.addSpacing(8)

        self.buttons: Dict[str, QPushButton] = {}
        for key, mnemonic, label, rule in FUNCTIONS:
            button = QPushButton(mnemonic)
            button.setObjectName("FuncButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setToolTip(f"{label} — CONF:{key}")
            button.clicked.connect(lambda _checked, k=key: self.functionSelected.emit(k))
            layout.addWidget(button)
            self.buttons[key] = button
            if rule:
                line = QFrame()
                line.setObjectName("HRule")
                line.setFrameShape(QFrame.HLine)
                layout.addSpacing(3)
                layout.addWidget(line)
                layout.addSpacing(3)
        layout.addStretch(1)

    def set_function(self, key: str) -> None:
        button = self.buttons.get(key)
        if button is not None and not button.isChecked():
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)


class NumberField(QLineEdit):
    """A numeric entry that commits on Enter, and never fights the user."""

    committed = Signal(float)

    def __init__(self, width: int = 96, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(width)
        self.returnPressed.connect(self._commit)
        self._last = ""

    def _commit(self) -> None:
        text = self.text().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            self.setText(self._last)
            return
        self._last = text
        self.committed.emit(value)

    def show_value(self, value: Any) -> None:
        if self.hasFocus():
            return
        text = trim_float(value) if is_num(value) else ""
        if self.text() != text:
            self.setText(text)
        self._last = text


class Softkey(QWidget):
    """One cell of the softkey row: label, control, SCPI-node caption."""

    def __init__(
        self, label: str, control: QWidget, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.control = control
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.title = QLabel(label)
        self.title.setObjectName("SectionLabel")
        self.title.setFont(theme.label())
        layout.addWidget(self.title)
        layout.addWidget(control)

        self.caption = QLabel("")
        self.caption.setObjectName("Caption")
        self.caption.setFont(theme.caption())
        layout.addWidget(self.caption)
        layout.addStretch(1)

    def set_node(self, node: str) -> None:
        if self.caption.text() != node:
            self.caption.setText(node)


class ControlStrip(QWidget):
    """The horizontal softkey row beneath the readout."""

    streamToggled = Signal(bool)
    singleRequested = Signal()
    configChanged = Signal(object)
    triggerChanged = Signal(object)
    mathChanged = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state: Optional[Dict[str, Any]] = None
        self._syncing = False
        self._keys: Dict[str, Softkey] = {}

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        outer.addWidget(self._acquire_block())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        scroll.setFixedHeight(74)

        body = QWidget()
        self._row = QHBoxLayout(body)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(14)
        self._build_keys()
        self._row.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    # ----------------------------------------------------------- construction

    def _acquire_block(self) -> QWidget:
        block = QWidget()
        block.setFixedWidth(206)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        heading = QLabel("ACQUIRE")
        heading.setObjectName("SectionLabel")
        heading.setFont(theme.label())
        layout.addWidget(heading)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("RunButton")
        self.run_button.setProperty("running", "false")
        self.run_button.clicked.connect(self._run_clicked)
        buttons.addWidget(self.run_button)

        self.single_button = QPushButton("Single")
        self.single_button.setToolTip("READ? — one reading, then the setup is restored")
        self.single_button.clicked.connect(self.singleRequested)
        buttons.addWidget(self.single_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.run_caption = QLabel("INIT · TRIG:COUN INF")
        self.run_caption.setObjectName("Caption")
        self.run_caption.setFont(theme.caption())
        layout.addWidget(self.run_caption)
        layout.addStretch(1)
        return block

    def _combo(self, on_change: Callable[[Any], None], width: int = 108) -> QComboBox:
        box = QComboBox()
        box.setFixedWidth(width)

        def changed(index: int) -> None:
            if self._syncing or index < 0:
                return
            on_change(box.itemData(index))

        box.currentIndexChanged.connect(changed)
        return box

    def _toggle(self, on_change: Callable[[bool], None], width: int = 88) -> QPushButton:
        button = QPushButton("Off")
        button.setObjectName("Toggle")
        button.setCheckable(True)
        button.setFixedWidth(width)

        def toggled(on: bool) -> None:
            button.setText("On" if on else "Off")
            if self._syncing:
                return
            on_change(on)

        button.toggled.connect(toggled)
        return button

    def _add(self, name: str, label: str, control: QWidget) -> Softkey:
        key = Softkey(label, control)
        self._row.addWidget(key)
        self._keys[name] = key
        return key

    def _build_keys(self) -> None:
        self.range_box = self._combo(
            lambda value: self.configChanged.emit({"range": value}), 118
        )
        self._add("range", "RANGE", self.range_box)

        self.auto_button = self._toggle(
            lambda on: self.configChanged.emit({"range_auto": on})
        )
        self._add("range_auto", "AUTO RANGE", self.auto_button)

        self.nplc_box = self._combo(
            lambda value: self.configChanged.emit({"nplc": value})
        )
        self._add("nplc", "INTEGRATION", self.nplc_box)

        self.aperture_box = self._combo(
            lambda value: self.configChanged.emit({"aperture": value})
        )
        self._add("aperture", "GATE TIME", self.aperture_box)

        self.azero_box = self._combo(
            lambda value: self.configChanged.emit({"azero": value})
        )
        for token in ("ON", "OFF", "ONCE"):
            self.azero_box.addItem(token, token)
        self._add("azero", "AUTOZERO", self.azero_box)

        self.impedance_box = self._combo(
            lambda value: self.configChanged.emit({"impedance": value})
        )
        self.impedance_box.addItem("10 MΩ", "10M")
        self.impedance_box.addItem("> 10 GΩ", "HIZ")
        self._add("impedance", "INPUT Z", self.impedance_box)

        self.band_box = self._combo(
            lambda value: self.configChanged.emit({"band": value})
        )
        for hz in (3, 20, 200):
            self.band_box.addItem(f"{hz} Hz", hz)
        self._add("band", "AC BANDWIDTH", self.band_box)

        self.probe_box = self._combo(
            lambda value: self.configChanged.emit({"temp_type": value})
        )
        for token, text in (
            ("FRTD", "4-wire RTD"),
            ("RTD", "2-wire RTD"),
            ("FTH", "4-wire therm."),
            ("THER", "Thermistor"),
        ):
            self.probe_box.addItem(text, token)
        self._add("temp_type", "PROBE", self.probe_box)

        self.temp_unit_box = self._combo(
            lambda value: self.configChanged.emit({"temp_unit": value}), 88
        )
        for token, text in (("C", "°C"), ("F", "°F"), ("K", "K")):
            self.temp_unit_box.addItem(text, token)
        self._add("temp_unit", "UNIT", self.temp_unit_box)

        self.rtd_field = NumberField(96)
        self.rtd_field.committed.connect(
            lambda value: self.configChanged.emit({"rtd_res": value})
        )
        self._add("rtd_res", "RTD R0", self.rtd_field)

        self.therm_box = self._combo(
            lambda value: self.configChanged.emit({"therm_type": value}), 96
        )
        self.therm_box.addItem("5000 Ω", 5000)
        self._add("therm_type", "THERMISTOR", self.therm_box)

        self.null_button = self._toggle(
            lambda on: self.mathChanged.emit({"null_on": on})
        )
        self._add("null_on", "NULL", self.null_button)

        self.null_field = NumberField(112)
        self.null_field.committed.connect(
            lambda value: self.mathChanged.emit({"null_value": value})
        )
        self._add("null_value", "NULL VALUE", self.null_field)

        self.null_auto_button = self._toggle(
            lambda on: self.mathChanged.emit({"null_auto": on})
        )
        self._add("null_auto", "NULL AUTO", self.null_auto_button)

        self.scale_button = self._toggle(
            lambda on: self.mathChanged.emit({"scale_on": on})
        )
        self._add("scale_on", "SCALING", self.scale_button)

        self.scale_box = self._combo(
            lambda value: self.mathChanged.emit({"scale_func": value})
        )
        # DB and DBM only.  CALC:SCAL:FUNC? also answers NULL — it is this
        # firmware's power-on value — but `CALC:SCAL:FUNC NULL` is refused
        # with -224,"Illegal parameter value" (SPEC.md section 2.1), so an
        # entry for it could do nothing except put an error in the
        # instrument's queue.  When the instrument reports NULL the box shows
        # nothing selected, which is the truth: no scaling function this
        # control can set is in force.  Nulling is the NULL / NULL VALUE /
        # NULL AUTO softkeys to the left, which use the per-function
        # <p>:NULL:* nodes and do work.
        for token in specs.SCALE_FUNCS:
            self.scale_box.addItem(token, token)
        self.scale_box.setToolTip(
            "CALC:SCAL:FUNC — dB or dBm. This instrument reports NULL when no "
            "scaling function has been chosen, but refuses to be set to it; "
            "use the NULL softkeys for nulling."
        )
        self._add("scale_func", "SCALE FUNC", self.scale_box)

        self.db_field = NumberField(96)
        self.db_field.committed.connect(
            lambda value: self.mathChanged.emit({"db_ref": value})
        )
        self._add("db_ref", "dB REF", self.db_field)

        self.dbm_field = NumberField(96)
        self.dbm_field.committed.connect(
            lambda value: self.mathChanged.emit({"dbm_ref": value})
        )
        self._add("dbm_ref", "dBm REF", self.dbm_field)

        self.trig_source_box = self._combo(
            lambda value: self.triggerChanged.emit({"source": value}), 96
        )
        for token in ("IMM", "BUS", "EXT"):
            self.trig_source_box.addItem(token, token)
        self._add("trig_source", "TRIGGER", self.trig_source_box)

        self.trig_slope_box = self._combo(
            lambda value: self.triggerChanged.emit({"slope": value}), 96
        )
        for token in ("POS", "NEG"):
            self.trig_slope_box.addItem(token, token)
        self._add("trig_slope", "SLOPE", self.trig_slope_box)

        self.trig_delay_field = NumberField(104)
        self.trig_delay_field.committed.connect(
            lambda value: self.triggerChanged.emit({"delay": value})
        )
        self._add("trig_delay", "TRIG DELAY", self.trig_delay_field)

        self.trig_delay_auto = self._toggle(
            lambda on: self.triggerChanged.emit({"delay_auto": on})
        )
        self._add("trig_delay_auto", "DELAY AUTO", self.trig_delay_auto)

        self.trig_count_field = QLineEdit()
        self.trig_count_field.setFixedWidth(96)
        self.trig_count_field.returnPressed.connect(self._commit_count)
        self._add("trig_count", "TRIG COUNT", self.trig_count_field)

        self.samples_field = NumberField(96)
        self.samples_field.committed.connect(
            lambda value: self.triggerChanged.emit({"samples": int(value)})
        )
        self._add("samples", "SAMPLES", self.samples_field)

    # ------------------------------------------------------------- behaviour

    def _run_clicked(self) -> None:
        running = bool(self._state and self._state.get("streaming"))
        self.streamToggled.emit(not running)

    def _commit_count(self) -> None:
        text = self.trig_count_field.text().strip().upper()
        if not text:
            return
        if text in ("INF", "INFINITY"):
            self.triggerChanged.emit({"count": "INF"})
            return
        try:
            self.triggerChanged.emit({"count": int(float(text))})
        except ValueError:
            self.trig_count_field.setText("")

    def _show(self, name: str, visible: bool) -> None:
        key = self._keys.get(name)
        if key is not None:
            key.setVisible(visible)

    def _select(self, box: QComboBox, value: Any) -> None:
        for index in range(box.count()):
            item = box.itemData(index)
            if item == value or (
                isinstance(item, (int, float))
                and is_num(value)
                and abs(float(item) - float(value)) <= abs(float(value)) * 1e-9
            ):
                box.setCurrentIndex(index)
                return
        box.setCurrentIndex(-1)

    def apply_state(self, state: Dict[str, Any]) -> None:
        self._state = state
        self._syncing = True
        try:
            self._apply(state)
        finally:
            self._syncing = False

    def _apply(self, state: Dict[str, Any]) -> None:
        func = str(state.get("func") or "")
        streaming = bool(state.get("streaming"))
        self.run_button.setText("Stop" if streaming else "Run")
        self.run_button.setProperty("running", "true" if streaming else "false")
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)
        self.run_caption.setText("ABOR" if streaming else "INIT · TRIG:COUN INF")

        ranges: List[float] = list(state.get("ranges") or [])
        has_range = bool(ranges)
        self._show("range", has_range)
        self._show("range_auto", has_range)
        if has_range:
            signature = (func, tuple(ranges))
            if self.range_box.property("signature") != str(signature):
                self.range_box.clear()
                for value in ranges:
                    self.range_box.addItem(fmt_range(value, state), value)
                self.range_box.setProperty("signature", str(signature))
            self._select(self.range_box, state.get("range"))
            self._sync_toggle(self.auto_button, state.get("range_auto"))
            self._keys["range"].set_node(node_for(func, "range"))
            self._keys["range_auto"].set_node(node_for(func, "range_auto"))

        has_nplc = is_num(state.get("nplc"))
        self._show("nplc", has_nplc)
        if has_nplc:
            options = list(state.get("nplc_options") or [])
            if self.nplc_box.count() != len(options):
                self.nplc_box.clear()
                for value in options:
                    self.nplc_box.addItem(f"{value:g} PLC", value)
            self._select(self.nplc_box, state.get("nplc"))
            self._keys["nplc"].set_node(node_for(func, "nplc"))

        has_aperture = func in ("FREQ", "PER") and is_num(state.get("aperture"))
        self._show("aperture", has_aperture)
        if has_aperture:
            options = list(state.get("aperture_options") or [])
            if self.aperture_box.count() != len(options):
                self.aperture_box.clear()
                for value in options:
                    self.aperture_box.addItem(f"{value:g} s", value)
            self._select(self.aperture_box, state.get("aperture"))
            self._keys["aperture"].set_node(node_for(func, "aperture"))

        azero = state.get("azero")
        self._show("azero", bool(azero))
        if azero:
            self._select(self.azero_box, str(azero).upper())
            self._keys["azero"].set_node(node_for(func, "azero"))

        impedance = state.get("impedance")
        self._show("impedance", bool(impedance))
        if impedance:
            self._select(self.impedance_box, str(impedance))
            self._keys["impedance"].set_node(node_for(func, "impedance"))

        band = state.get("band")
        self._show("band", is_num(band))
        if is_num(band):
            self._select(self.band_box, int(float(band)))
            self._keys["band"].set_node(node_for(func, "band"))

        temp = state.get("temp")
        probe = str((temp or {}).get("type") or "").upper()
        is_rtd = probe in ("RTD", "FRTD")
        for name in ("temp_type", "temp_unit"):
            self._show(name, bool(temp))
        self._show("rtd_res", bool(temp) and is_rtd)
        self._show("therm_type", bool(temp) and not is_rtd)
        if temp:
            self._select(self.probe_box, probe)
            self._select(self.temp_unit_box, str(temp.get("unit") or "").upper())
            self.rtd_field.show_value(temp.get("rtd_res"))
            therm = temp.get("therm_type")
            if therm is not None:
                try:
                    self._select(self.therm_box, int(float(therm)))
                except ValueError:
                    self.therm_box.setCurrentIndex(-1)
            self._keys["temp_type"].set_node(node_for(func, "temp_type"))
            self._keys["temp_unit"].set_node(node_for(func, "temp_unit"))
            self._keys["rtd_res"].set_node(node_for(func, "rtd_res"))
            self._keys["therm_type"].set_node(node_for(func, "therm_type"))

        math_state = state.get("math") or {}
        has_null = math_state.get("null_on") is not None
        for name in ("null_on", "null_value", "null_auto"):
            self._show(name, has_null)
        if has_null:
            self._sync_toggle(self.null_button, math_state.get("null_on"))
            self.null_field.show_value(math_state.get("null_value"))
            self._sync_toggle(self.null_auto_button, math_state.get("null_auto"))
            self._keys["null_on"].set_node(node_for(func, "null_on"))
            self._keys["null_value"].set_node(node_for(func, "null_value"))
            self._keys["null_auto"].set_node(node_for(func, "null_auto"))

        self._sync_toggle(self.scale_button, math_state.get("scale_on"))
        # No fallback token here: an instrument reporting NULL selects nothing,
        # because NULL is not a value this control can send back.
        self._select(self.scale_box, str(math_state.get("scale_func") or "").upper())
        self.db_field.show_value(math_state.get("db_ref"))
        self.dbm_field.show_value(math_state.get("dbm_ref"))
        for name in ("scale_on", "scale_func", "db_ref", "dbm_ref"):
            self._keys[name].set_node(node_for(func, name))

        trigger = state.get("trigger") or {}
        self._select(self.trig_source_box, str(trigger.get("source") or "IMM").upper())
        self._select(self.trig_slope_box, str(trigger.get("slope") or "POS").upper())
        self.trig_delay_field.show_value(trigger.get("delay"))
        self._sync_toggle(self.trig_delay_auto, trigger.get("delay_auto"))
        count = trigger.get("count")
        if not self.trig_count_field.hasFocus():
            self.trig_count_field.setText("" if count is None else str(count))
        self.samples_field.show_value(trigger.get("samples"))
        for name in (
            "trig_source",
            "trig_slope",
            "trig_delay",
            "trig_delay_auto",
            "trig_count",
            "samples",
        ):
            self._keys[name].set_node(
                node_for(
                    func,
                    {
                        "trig_source": "source",
                        "trig_slope": "slope",
                        "trig_delay": "delay",
                        "trig_delay_auto": "delay_auto",
                        "trig_count": "count",
                        "samples": "samples",
                    }[name],
                )
            )

    def _sync_toggle(self, button: QPushButton, value: Any) -> None:
        on = bool(value)
        button.setChecked(on)
        button.setText("On" if on else "Off")

    def set_enabled_for_link(self, connected: bool) -> None:
        for key in self._keys.values():
            key.setEnabled(connected)
        self.run_button.setEnabled(connected)
        self.single_button.setEnabled(connected)
