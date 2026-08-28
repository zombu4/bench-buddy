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

"""The SCPI console — the expert tool.

Any command may be sent, including the ones SPEC.md section 2.2 says hang this
instrument's socket, because the console is where an engineer deliberately
probes.  Those commands are named on screen and need a second press before they
go out.  The instrument layer fences them with a 3 s timeout and rebuilds the
link, so a stalled query costs one confirmation and three seconds, not a
restart.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from ..scpi import FORBIDDEN

CHIPS = (
    "*IDN?",
    "SYST:ERR?",
    "SENS:FUNC?",
    "CONF?",
    "DATA:POIN?",
    "DATA:LAST?",
    "SYST:UPT?",
    "STAT:QUES:COND?",
)
MAX_LINES = 4000


class CommandLine(QLineEdit):
    """A command entry with history on the arrow keys."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._history: List[str] = []
        self._index = -1

    def remember(self, command: str) -> None:
        if not command:
            return
        if not self._history or self._history[-1] != command:
            self._history.append(command)
        del self._history[:-200]
        self._index = len(self._history)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Up and self._history:
            self._index = max(0, self._index - 1)
            self.setText(self._history[self._index])
            self.selectAll()
            return
        if event.key() == Qt.Key_Down and self._history:
            self._index = min(len(self._history), self._index + 1)
            self.setText(
                "" if self._index >= len(self._history) else self._history[self._index]
            )
            return
        super().keyPressEvent(event)


class ConsolePanel(QWidget):
    """Command entry, transcript, and the unsupported-command confirmation."""

    send = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pending: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        for command in CHIPS:
            button = QPushButton(command)
            button.setObjectName("Seg")
            button.clicked.connect(lambda _checked, c=command: self._chip(c))
            chips.addWidget(button)
        chips.addStretch(1)
        layout.addLayout(chips)

        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setMaximumBlockCount(MAX_LINES)
        self.transcript.setFont(theme.mono(12))
        self.transcript.setPlaceholderText(
            "Every command sent and every reply received appears here."
        )
        layout.addWidget(self.transcript, 1)

        entry = QHBoxLayout()
        entry.setSpacing(6)
        prompt = QLabel("scpi ›")
        prompt.setObjectName("Caption")
        prompt.setFont(theme.mono(12, QFont.Medium))
        entry.addWidget(prompt)
        self.input = CommandLine()
        self.input.setPlaceholderText("type a command and press Enter")
        self.input.returnPressed.connect(self._submit)
        self.input.textEdited.connect(self._typed)
        entry.addWidget(self.input, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("Primary")
        self.send_button.clicked.connect(self._submit)
        entry.addWidget(self.send_button)
        layout.addLayout(entry)

        self.warning = QLabel("")
        self.warning.setObjectName("Caption")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        self._print(
            "note",
            "Anything may be sent here. Commands on the never-send list of "
            "SPEC.md 2.2 ask for a second press first — they stall this "
            "instrument's socket, and recovery costs a link reset.",
        )

    # ---------------------------------------------------------------- input

    def _chip(self, command: str) -> None:
        self.input.setText(command)
        self.input.setFocus()

    def _typed(self, _text: str) -> None:
        if self._pending is not None:
            self._pending = None
        self._update_warning()

    def _unsupported(self, command: str) -> Optional[str]:
        for piece in command.split(";"):
            head = piece.strip().split(" ")[0].strip().upper().lstrip(":")
            if head in FORBIDDEN:
                return head
        return None

    def _update_warning(self) -> None:
        head = self._unsupported(self.input.text().strip())
        if head is None:
            self.warning.setText("")
            self.warning.setStyleSheet("")
            return
        if self._pending is not None:
            self.warning.setText(
                f"{head} is verified to hang this instrument's socket. "
                "Press Enter again to send it anyway; the link will be reset "
                "after 3 s and an in-progress run will be stopped."
            )
            self.warning.setStyleSheet(f"color: {theme.FAIL};")
        else:
            self.warning.setText(
                f"{head} is on the never-send list (SPEC.md 2.2). "
                "Sending it needs a second press."
            )
            self.warning.setStyleSheet(f"color: {theme.WARN};")

    def _submit(self) -> None:
        command = self.input.text().strip()
        if not command:
            return
        head = self._unsupported(command)
        if head is not None and self._pending != command:
            self._pending = command
            self._update_warning()
            return
        self._pending = None
        self.warning.setText("")
        self.warning.setStyleSheet("")
        self.input.remember(command)
        self.input.clear()
        self._print("out", command)
        self.send.emit(command)

    # --------------------------------------------------------------- output

    def show_result(self, result: Dict[str, Any]) -> None:
        elapsed = result.get("elapsed_ms")
        suffix = f"   [{elapsed:.1f} ms]" if isinstance(elapsed, (int, float)) else ""
        error = result.get("error")
        if error:
            self._print("err", f"{error}{suffix}")
            return
        if result.get("is_query"):
            self._print("in", f"{result.get('response', '')}{suffix}")
        else:
            self._print("ok", f"sent{suffix}")

    def note(self, text: str) -> None:
        self._print("note", text)

    def _print(self, kind: str, text: str) -> None:
        colour = {
            "out": theme.SIGNAL,
            "in": theme.PHOSPHOR,
            "ok": theme.OK,
            "err": theme.FAIL,
            "note": theme.DIM,
        }.get(kind, theme.TEXT)
        marker = {
            "out": "›",
            "in": "‹",
            "ok": "·",
            "err": "!",
            "note": "#",
        }.get(kind, " ")
        escaped = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        self.transcript.appendHtml(
            f'<span style="color:{theme.DIM}">{marker} </span>'
            f'<span style="color:{colour}">{escaped}</span>'
        )
        self.transcript.moveCursor(QTextCursor.End)
