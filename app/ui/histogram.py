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

"""The instrument's own histogram, from ``CALC:TRAN:HIST:ALL?``.

The bins are the 34461A's, never recomputed here.  When the instrument has not
determined its bounds yet it answers with the 9.91E37 sentinel; the instrument
layer nulls those, and this widget says so rather than drawing a decade-37 axis.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .chart import nice_step
from .strip import NumberField, Softkey
from .units import fmt_si, is_num, max_exponent, unit_no_prefix, unit_text

PAD_LEFT = 62
PAD_RIGHT = 16
PAD_TOP = 12
PAD_BOTTOM = 30


class HistogramWidget(QWidget):
    """Bar renderer for the instrument's histogram bins."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(200)
        self._data: Optional[Dict[str, Any]] = None
        self.unit = ""
        self.no_prefix = False
        self.max_exp: Optional[int] = None
        self.empty_text = (
            "Enable the histogram (CALC:TRAN:HIST:STAT) and run — the "
            "instrument's own bins appear here."
        )

    def set_unit(self, unit: str, no_prefix: bool, max_exp: Optional[int]) -> None:
        self.unit = unit
        self.no_prefix = no_prefix
        self.max_exp = max_exp
        self.update()

    def set_data(self, data: Optional[Dict[str, Any]]) -> None:
        self._data = data
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), theme.C["ink"])

        plot = QRectF(
            PAD_LEFT,
            PAD_TOP,
            max(1.0, self.width() - PAD_LEFT - PAD_RIGHT),
            max(1.0, self.height() - PAD_TOP - PAD_BOTTOM),
        )

        data = self._data
        bins: List[int] = list(data.get("bins") or []) if data else []
        if not bins or not data:
            painter.setFont(theme.sans(12))
            painter.setPen(theme.C["dim"])
            painter.drawText(plot, int(Qt.AlignCenter), self.empty_text)
            painter.setPen(QPen(theme.C["rule"], 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(plot.adjusted(0.5, 0.5, -0.5, -0.5))
            painter.end()
            return

        lower = data.get("lower")
        upper = data.get("upper")
        peak = max(bins) if bins else 0
        if peak <= 0:
            peak = 1

        painter.setFont(theme.mono(10))
        # Bin counts are integers, so the tick step must be one too: a 0.2
        # step on a peak of 1 would label five rows "0".
        step = max(1.0, round(nice_step(peak / 4.0)))
        value = 0.0
        while value <= peak * 1.0001:
            y = plot.bottom() - (value / peak) * plot.height()
            painter.setPen(QPen(theme.C["rule"], 1))
            painter.drawLine(
                QPointF(plot.left(), round(y) + 0.5),
                QPointF(plot.right(), round(y) + 0.5),
            )
            painter.setPen(theme.C["dim"])
            painter.drawText(
                QRectF(0, y - 8, PAD_LEFT - 8, 16),
                int(Qt.AlignRight | Qt.AlignVCenter),
                f"{int(value)}",
            )
            value += step

        width = plot.width() / len(bins)
        fill = QColor(theme.SIGNAL)
        fill.setAlpha(190)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        for index, count in enumerate(bins):
            if count <= 0:
                continue
            height = (count / peak) * plot.height()
            bar = QRectF(
                plot.left() + index * width,
                plot.bottom() - height,
                max(1.0, width - 1.0),
                height,
            )
            painter.drawRect(bar)

        painter.setPen(QPen(theme.C["rule"], 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(plot.adjusted(0.5, 0.5, -0.5, -0.5))

        painter.setFont(theme.mono(10))
        painter.setPen(theme.C["dim"])
        axis = QRectF(plot.left(), plot.bottom() + 4, plot.width(), 16)
        if is_num(lower) and is_num(upper):
            painter.drawText(
                axis,
                int(Qt.AlignLeft | Qt.AlignVCenter),
                fmt_si(lower, self.unit, 5, self.no_prefix, self.max_exp),
            )
            painter.drawText(
                axis,
                int(Qt.AlignRight | Qt.AlignVCenter),
                fmt_si(upper, self.unit, 5, self.no_prefix, self.max_exp),
            )
            middle = (float(lower) + float(upper)) / 2.0
            painter.drawText(
                axis,
                int(Qt.AlignCenter),
                fmt_si(middle, self.unit, 5, self.no_prefix, self.max_exp),
            )
        else:
            painter.drawText(
                axis,
                int(Qt.AlignLeft | Qt.AlignVCenter),
                "the instrument has not determined its histogram bounds yet",
            )
        painter.end()


class HistogramPanel(QWidget):
    """The Histogram tab: the instrument's own bins and the controls behind them."""

    mathChanged = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.view = HistogramWidget(self)
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.setSpacing(14)

        self.enable = QPushButton("Off")
        self.enable.setObjectName("Toggle")
        self.enable.setCheckable(True)
        self.enable.toggled.connect(self._enabled)
        self._enable_key = Softkey("HISTOGRAM", self.enable)
        self._enable_key.set_node("CALC:TRAN:HIST:STAT")
        controls.addWidget(self._enable_key)

        self.points = QComboBox()
        self.points.setFixedWidth(96)
        for count in (10, 20, 40, 100, 200, 400):
            self.points.addItem(str(count), count)
        self.points.currentIndexChanged.connect(self._points_changed)
        points_key = Softkey("BINS", self.points)
        points_key.set_node("CALC:TRAN:HIST:POIN")
        controls.addWidget(points_key)

        self.auto = QPushButton("Off")
        self.auto.setObjectName("Toggle")
        self.auto.setCheckable(True)
        self.auto.toggled.connect(self._auto_changed)
        auto_key = Softkey("AUTO RANGE", self.auto)
        auto_key.set_node("CALC:TRAN:HIST:RANG:AUTO")
        controls.addWidget(auto_key)

        self.lower = NumberField(112)
        self.lower.committed.connect(
            lambda value: self.mathChanged.emit({"hist_low": value})
        )
        lower_key = Softkey("LOWER", self.lower)
        lower_key.set_node("CALC:TRAN:HIST:RANG:LOW")
        controls.addWidget(lower_key)

        self.upper = NumberField(112)
        self.upper.committed.connect(
            lambda value: self.mathChanged.emit({"hist_high": value})
        )
        upper_key = Softkey("UPPER", self.upper)
        upper_key.set_node("CALC:TRAN:HIST:RANG:UPP")
        controls.addWidget(upper_key)

        controls.addStretch(1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("CALC:TRAN:HIST:ALL?")
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("CALC:TRAN:HIST:CLE")
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.clear_button)
        layout.addLayout(controls)

        self.summary = QLabel("—")
        self.summary.setObjectName("Value")
        layout.addWidget(self.summary)
        layout.addWidget(self.view, 1)

    def _enabled(self, on: bool) -> None:
        self.enable.setText("On" if on else "Off")
        if not self._syncing:
            self.mathChanged.emit({"hist_on": on})

    def _auto_changed(self, on: bool) -> None:
        self.auto.setText("On" if on else "Off")
        if not self._syncing:
            self.mathChanged.emit({"hist_auto": on})

    def _points_changed(self, index: int) -> None:
        if self._syncing or index < 0:
            return
        self.mathChanged.emit({"hist_points": self.points.itemData(index)})

    def clear(self) -> None:
        """Drop the bins and the summary; they belong to one instrument."""
        self.view.set_data(None)
        self.summary.setText("—")
        self._syncing = True
        self.enable.setChecked(False)
        self.enable.setText("Off")
        self.auto.setChecked(False)
        self.auto.setText("Off")
        self.lower.show_value(None)
        self.upper.show_value(None)
        self._syncing = False

    def set_data(self, data: Dict[str, Any], state: Optional[Dict[str, Any]]) -> None:
        self.view.set_data(data)
        unit = unit_text(state)
        lower = data.get("lower")
        upper = data.get("upper")
        bounds = (
            f"{fmt_si(lower, unit, 5, unit_no_prefix(state), max_exponent(unit))}"
            f"  …  {fmt_si(upper, unit, 5, unit_no_prefix(state), max_exponent(unit))}"
            if is_num(lower) and is_num(upper)
            else "bounds not determined"
        )
        self.summary.setText(
            f"{data.get('count', 0)} readings   ·   {len(data.get('bins') or [])} bins"
            f"   ·   {bounds}"
        )

    def apply_state(self, state: Dict[str, Any]) -> None:
        unit = unit_text(state)
        self.view.set_unit(unit, unit_no_prefix(state), max_exponent(unit))
        math_state = state.get("math") or {}
        self._syncing = True
        try:
            on = bool(math_state.get("hist_on"))
            self.enable.setChecked(on)
            self.enable.setText("On" if on else "Off")
            auto = bool(math_state.get("hist_auto"))
            self.auto.setChecked(auto)
            self.auto.setText("On" if auto else "Off")
            points = math_state.get("hist_points")
            if points is not None:
                index = self.points.findData(int(points))
                if index < 0:
                    self.points.addItem(str(int(points)), int(points))
                    index = self.points.count() - 1
                self.points.setCurrentIndex(index)
            self.lower.show_value(math_state.get("hist_low"))
            self.upper.show_value(math_state.get("hist_high"))
        finally:
            self._syncing = False
        # The instrument owns the bounds while auto-ranging is on.
        self.lower.setEnabled(not bool(math_state.get("hist_auto")))
        self.upper.setEnabled(not bool(math_state.get("hist_auto")))
