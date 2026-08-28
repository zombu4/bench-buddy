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

"""A single, explicit capture of the instrument screen.

Continuous mirroring is withdrawn (IO-DISCIPLINE.md rule 4).  It re-displayed
information this application already holds and renders natively — function,
range, reading, units, trigger mode, NPLC, every math state — and at 277 KB a
frame it was the heaviest single load on the instrument's Windows CE LAN stack,
which is what degraded the hardware.

What remains is a deliberate one-per-click grab for documentation and
reporting.  There is no capture thread, no timer and no polling behind this
widget.  Because the frame is a snapshot of one instant and not a live view,
everything here is labelled with *when it was taken* and never presented as
current: the caption states the capture time, and once the frame is more than a
few seconds old the panel says so plainly rather than letting a still image
imply the meter is doing now what it was doing then.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme

SCREEN_W = 480
SCREEN_H = 289
VIEWS = (
    ("NUM", "Number"),
    ("TCH", "Trend"),
    ("HIST", "Histogram"),
    ("MET", "Bar meter"),
)
# Past this the caption stops giving an age in seconds and starts giving the
# wall-clock time it was taken, so nobody reads a stale frame as live.
STALE_AFTER_S = 10.0


class CaptureView(QWidget):
    """Scale-to-fit blit of the captured frame, aspect preserved, no smoothing."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(150)
        self._image: Optional[QImage] = None
        self._message = "No capture yet. Press Capture screen for a snapshot."

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return int(round(width * SCREEN_H / SCREEN_W))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def set_image(self, image: Optional[QImage]) -> None:
        self._image = image
        self.update()

    def set_message(self, message: str) -> None:
        self._message = message
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        rect = QRectF(self.rect())
        painter.fillRect(rect, theme.C["glass"])

        image = self._image
        if image is None or image.isNull():
            painter.setFont(theme.sans(11))
            painter.setPen(theme.C["dim"])
            painter.drawText(
                rect.adjusted(12, 0, -12, 0),
                int(Qt.AlignCenter | Qt.TextWordWrap),
                self._message,
            )
        else:
            scale = min(rect.width() / SCREEN_W, rect.height() / SCREEN_H)
            width = SCREEN_W * scale
            height = SCREEN_H * scale
            target = QRectF(
                rect.left() + (rect.width() - width) / 2.0,
                rect.top() + (rect.height() - height) / 2.0,
                width,
                height,
            )
            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
            painter.drawImage(target, image)
        painter.setPen(QPen(theme.C["rule"], 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()


class CapturePanel(QWidget):
    """The snapshot, its capture button, the instrument's view switch, and save."""

    captureRequested = Signal()
    viewSelected = Signal(str)
    saveRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.view = CaptureView(self)
        self._frame: Optional[bytes] = None
        self._frame_time = 0.0
        self._current_view = ""
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("SCREEN CAPTURE")
        title.setObjectName("SectionLabel")
        title.setFont(theme.label())
        header.addWidget(title)
        header.addStretch(1)
        self.age = QLabel("no capture")
        self.age.setObjectName("Caption")
        header.addWidget(self.age)
        layout.addLayout(header)

        layout.addWidget(self.view)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.capture_button = QPushButton("Capture screen")
        self.capture_button.setToolTip(
            "Take one snapshot of the instrument display with HCOP:SDUM:DATA?.\n"
            "One grab per press — the app does not poll the screen."
        )
        self.capture_button.clicked.connect(self._on_capture)
        controls.addWidget(self.capture_button)
        self.save_button = QPushButton("Save PNG")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.saveRequested)
        controls.addWidget(self.save_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        views = QGridLayout()
        views.setSpacing(4)
        self.view_buttons: Dict[str, QPushButton] = {}
        for index, (token, label) in enumerate(VIEWS):
            button = QPushButton(label)
            button.setObjectName("Seg")
            button.setCheckable(True)
            button.setToolTip(f"DISP:VIEW {token}")
            button.clicked.connect(lambda _checked, t=token: self.viewSelected.emit(t))
            views.addWidget(button, index // 2, index % 2)
            self.view_buttons[token] = button
        layout.addLayout(views)

        caption = QLabel("DISP:VIEW · HCOP:SDUM:DATA?")
        caption.setObjectName("Caption")
        layout.addWidget(caption)

        note = QLabel(
            "A snapshot, not a live view. The readout, chart and panels above "
            "are the live data."
        )
        note.setObjectName("Caption")
        note.setWordWrap(True)
        layout.addWidget(note)

    # ---------------------------------------------------------------- input

    def _on_capture(self) -> None:
        self._busy = True
        self.capture_button.setEnabled(False)
        self.age.setText("capturing…")
        self.captureRequested.emit()

    def set_frame(self, png: bytes, stamp: float) -> None:
        self._busy = False
        self.capture_button.setEnabled(True)
        image = QImage()
        if not image.loadFromData(bytes(png), "PNG"):
            self.view.set_message(
                "The instrument returned a frame this build could not decode."
            )
            return
        self._frame = bytes(png)
        self._frame_time = stamp
        self.save_button.setEnabled(True)
        self.view.set_image(image)

    def capture_failed(self) -> None:
        """Re-enable the button after a grab that did not produce a frame."""
        self._busy = False
        self.capture_button.setEnabled(True)

    def clear_frame(self) -> None:
        """Forget the captured frame, for when the link changes instrument.

        A screen grab is a snapshot of one particular meter.  Leaving it on
        screen after connecting to a different one would present it as though
        it came from the instrument now attached.
        """
        self._busy = False
        self._frame = None
        self._frame_time = 0.0
        self.save_button.setEnabled(False)
        self.view.set_image(None)
        self.view.set_message("No capture yet. Press Capture screen for a snapshot.")
        self.age.setText("no capture")

    def latest_frame(self) -> Optional[bytes]:
        return self._frame

    def apply_state(self, state: Dict[str, Any]) -> None:
        display = state.get("display") or {}
        view = str(display.get("view") or "").upper()
        if view != self._current_view:
            self._current_view = view
            for token, button in self.view_buttons.items():
                button.setChecked(token == view)

    def set_enabled_for_link(self, connected: bool) -> None:
        self.capture_button.setEnabled(connected and not self._busy)
        for button in self.view_buttons.values():
            button.setEnabled(connected)

    def tick(self) -> None:
        """Keep the capture caption honest about the frame's age."""
        if self._busy:
            return
        if self._frame_time <= 0:
            self.age.setText("no capture")
            return
        age = max(0.0, time.time() - self._frame_time)
        if age <= STALE_AFTER_S:
            self.age.setText(f"captured {age:.0f} s ago")
        else:
            taken = time.strftime("%H:%M:%S", time.localtime(self._frame_time))
            self.age.setText(f"snapshot from {taken}")
