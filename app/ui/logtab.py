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

"""The data log: what the instrument recorded, and the CSV export.

The table is a rolling view of the readings this session has seen; the export
is streamed from the instrument model's own log buffer, which holds up to two
million points and is the authoritative record.  An overloaded sample is an
explicit ``OVLD`` cell in both, never an empty one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .units import fmt_state

VIEW_ROWS = 20000  # the table's rolling window; the export is not limited
COLUMNS = ("#", "ELAPSED", "READING", "RAW")


class LogModel(QAbstractTableModel):
    """A rolling window over the readings, with OVLD rendered explicitly."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: List[Tuple[int, float, Optional[float]]] = []
        self._first_index = 0
        self._state: Optional[Dict[str, Any]] = None
        self._t0: Optional[float] = None
        self._prev_rel = 0.0

    def set_state(self, state: Dict[str, Any]) -> None:
        self._state = state

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self._first_index = 0
        self._t0 = None
        self._prev_rel = 0.0
        self.endResetModel()

    def append(
        self, timestamp: float, values: List[Optional[float]], rate_hz: float
    ) -> None:
        """Add one published batch.

        The wire format carries one timestamp per batch, so sample times are
        interpolated across it exactly as the chart does — otherwise every
        reading in a 400-sample block would claim the same instant.
        """
        if not values:
            return
        count = len(values)
        if self._t0 is None:
            lead = count / rate_hz if rate_hz > 0 else count * 0.01
            self._t0 = timestamp - lead
            self._prev_rel = 0.0
        t_rel = timestamp - self._t0
        previous = self._prev_rel
        if t_rel <= previous:
            t_rel = previous + (count / rate_hz if rate_hz > 0 else 0.001 * count)
        step = (t_rel - previous) / count

        base = self._first_index + len(self._rows)
        start = len(self._rows)
        self.beginInsertRows(QModelIndex(), start, start + count - 1)
        for offset, value in enumerate(values):
            self._rows.append((base + offset, previous + step * (offset + 1), value))
        self.endInsertRows()
        self._prev_rel = t_rel

        excess = len(self._rows) - VIEW_ROWS
        if excess > 0:
            self.beginRemoveRows(QModelIndex(), 0, excess - 1)
            del self._rows[:excess]
            self._first_index += excess
            self.endRemoveRows()

    # ------------------------------------------------------------ Qt model

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        number, elapsed, value = self._rows[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            if column == 0:
                return str(number)
            if column == 1:
                return f"{elapsed:.4f} s"
            if column == 2:
                return "OVLD" if value is None else fmt_state(value, self._state, 7)
            return "OVLD" if value is None else repr(value)
        if role == Qt.ForegroundRole and value is None and column >= 2:
            return QColor(theme.FAIL)
        if role == Qt.TextAlignmentRole and column != 2:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class LogPanel(QWidget):
    """Recording controls, the rolling table and the CSV export."""

    recordToggled = Signal(bool, str)
    clearRequested = Signal()
    exportRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = LogModel(self)
        self._pending: List[Tuple[float, List[Optional[float]], float]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.record_button = QPushButton("Record")
        self.record_button.setObjectName("Toggle")
        self.record_button.setCheckable(True)
        self.record_button.toggled.connect(self._record)
        controls.addWidget(self.record_button)

        self.note = QLineEdit()
        self.note.setPlaceholderText("note stored in the CSV header")
        self.note.setMaximumWidth(320)
        controls.addWidget(self.note)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clearRequested)
        controls.addWidget(self.clear_button)

        self.export_button = QPushButton("Export CSV…")
        self.export_button.setObjectName("Primary")
        self.export_button.clicked.connect(self.exportRequested)
        controls.addWidget(self.export_button)

        controls.addStretch(1)
        self.count = QLabel("0 readings recorded")
        self.count.setObjectName("Value")
        controls.addWidget(self.count)
        layout.addLayout(controls)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.setFont(theme.mono(12))
        layout.addWidget(self.table, 1)

        # An empty table should say what fills it, not just sit there blank.
        self.empty = QLabel(
            "Press Record — every reading the instrument takes lands here and "
            "in the CSV export. An overloaded sample is written as OVLD, never "
            "as an empty cell."
        )
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {theme.DIM};")
        self.empty.setParent(self.table.viewport())
        self.table.viewport().installEventFilter(self)
        self._place_empty()

        self.caption = QLabel(
            "The table holds the most recent "
            f"{VIEW_ROWS:,} readings; the export writes every recorded point."
        )
        self.caption.setObjectName("Caption")
        layout.addWidget(self.caption)

        self._follow = True
        self.table.verticalScrollBar().valueChanged.connect(self._scrolled)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.table.viewport() and event.type() == QEvent.Resize:
            self._place_empty()
        return super().eventFilter(watched, event)

    def _place_empty(self) -> None:
        viewport = self.table.viewport()
        self.empty.setGeometry(
            24, max(0, viewport.height() // 2 - 40), max(80, viewport.width() - 48), 80
        )
        self.empty.setVisible(self.model.rowCount() == 0)

    def _scrolled(self, value: int) -> None:
        bar = self.table.verticalScrollBar()
        self._follow = value >= bar.maximum() - 2

    def _record(self, on: bool) -> None:
        self.recordToggled.emit(on, self.note.text().strip())

    def buffer(
        self, timestamp: float, values: List[Optional[float]], rate_hz: float
    ) -> None:
        """Queue a batch; the 30 Hz timer drains it into the model."""
        self._pending.append((timestamp, values, rate_hz))

    def tick(self) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        for timestamp, values, rate_hz in pending:
            self.model.append(timestamp, values, rate_hz)
        self.empty.setVisible(self.model.rowCount() == 0)
        if self._follow:
            self.table.scrollToBottom()

    def clear(self) -> None:
        self._pending = []
        self.model.clear()
        self.empty.setVisible(True)

    def apply_state(self, state: Dict[str, Any]) -> None:
        self.model.set_state(state)
        recording = bool(state.get("logging"))
        if recording != self.record_button.isChecked():
            self.record_button.blockSignals(True)
            self.record_button.setChecked(recording)
            self.record_button.blockSignals(False)
        self.record_button.setText("Recording" if recording else "Record")
        count = int(state.get("log_count") or 0)
        text = f"{count:,} readings recorded"
        if state.get("log_overflow"):
            text += " — buffer full, further readings are not logged"
            self.count.setStyleSheet(f"color: {theme.WARN};")
        else:
            self.count.setStyleSheet("")
        self.count.setText(text)
