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

"""The saved-instrument library and the connection dialog.

Before this existed the instrument address could only be given as
``--instrument`` or ``DMM_HOST``, so an installed copy launched from the Start
Menu was pinned to whatever address the build happened to default to.  A bench
usually has more than one meter, so what is kept is a small library rather than
a single address: each entry carries a label, an address, a transport and the
``--finite-trigger-count`` flag, and the whole list plus the last-connected
entry is persisted with :class:`QSettings`.

Nothing here talks to the instrument.  The dialog produces a
:class:`SavedInstrument`; :mod:`app.ui.main` decides what to do with it and
:mod:`app.ui.bridge` is the only thing that opens a link.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

#: The transports ``--transport`` accepts, in the same order and with the same
#: meanings.  ``auto`` prefers VXI-11 because the instrument ends the
#: acquisition by itself when a VXI-11 link dies (IO-DISCIPLINE.md rule 1).
TRANSPORTS = (
    ("auto", "auto — prefer VXI-11, fall back to the raw socket"),
    ("vxi11", "vxi11 — the instrument stops acquiring if this app is killed"),
    ("socket", "socket — raw TCP 5025, no session semantics"),
)
TRANSPORT_KEYS = tuple(key for key, _text in TRANSPORTS)

#: The label a new entry starts with.  While an entry still carries it the
#: label is replaced by the instrument's real identity on the first successful
#: connection; a label the user has typed is never overwritten.
DEFAULT_LABEL = "New instrument"

#: Shown as placeholder text only.  Nothing connects to it on its own — with
#: nothing saved and no ``--instrument``, the application shows this dialog
#: rather than guessing at an address.
EXAMPLE_ADDRESS = "192.0.2.50"

SETTINGS_LIST = "instruments/list"
SETTINGS_SELECTED = "instruments/selected"

# A DNS label: letters, digits and hyphens, not starting or ending with one.
# Deliberately permissive — the point is to catch a typed mistake such as an
# empty box or a pasted "http://host/", not to decide what a valid hostname is.
# Underscores are accepted because they appear in real internal names.
_LABEL_RE = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?$")


def validate_address(address: str) -> Optional[str]:
    """Return a human-readable reason *address* is unusable, or None.

    Hostnames, IPv4 literals and IPv6 literals all pass.  This does not try to
    decide whether the address exists — only whether it is worth attempting a
    connection to.  Anything stricter would reject legitimate hostnames, which
    is the failure mode that matters here: a user on a bench with DNS should
    be able to type ``dmm-bench-2``.
    """
    text = (address or "").strip()
    if not text:
        return "Enter the instrument's hostname or IP address."
    if len(text) > 255:
        return "That address is too long to be a hostname."
    if any(ch.isspace() for ch in text):
        return "An address cannot contain spaces."
    for bad, why in (
        ("/", "Enter just the host, with no http:// and no path."),
        ("\\", "Enter just the host, with no backslashes."),
        ("@", "Enter just the host, with no user@ prefix."),
        (",", "Enter one address only."),
    ):
        if bad in text:
            return why
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "That address contains characters that are not printable."

    literal = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    if ":" in literal:
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            return (
                "A colon is only allowed in an IPv6 address, and that is not "
                "one. The SCPI port is not part of the address."
            )
        return None

    parts = text.split(".")
    if any(not part for part in parts):
        return "That address has an empty part between its dots."
    for part in parts:
        if len(part) > 63:
            return "One part of that hostname is longer than 63 characters."
        if not _LABEL_RE.match(part):
            return (
                f"{part!r} is not a valid hostname part — use letters, digits "
                f"and hyphens."
            )
    return None


def label_from_identity(identity: Dict[str, Any]) -> str:
    """``34461A MY12345678`` from a parsed ``*IDN?``, or "" if unusable."""
    model = str(identity.get("model") or "").strip()
    serial = str(identity.get("serial") or "").strip()
    if model and serial:
        return f"{model} {serial}"
    return model or serial or ""


class SavedInstrument:
    """One entry in the library.

    ``auto_label`` records whether the label is still the application's rather
    than the user's.  It starts True, is cleared the moment the user types in
    the label box, and gates the auto-labelling in
    :meth:`app.ui.main.MainWindow._on_identity` — so a hand-typed
    "Bench 2, calibration" survives every reconnection.
    """

    __slots__ = ("label", "address", "transport", "finite", "auto_label")

    def __init__(
        self,
        label: str = DEFAULT_LABEL,
        address: str = "",
        transport: str = "auto",
        finite: bool = False,
        auto_label: bool = True,
    ) -> None:
        self.label = label or DEFAULT_LABEL
        self.address = address
        self.transport = transport if transport in TRANSPORT_KEYS else "auto"
        self.finite = bool(finite)
        self.auto_label = bool(auto_label)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "address": self.address,
            "transport": self.transport,
            "finite": self.finite,
            "auto_label": self.auto_label,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SavedInstrument":
        return cls(
            label=str(raw.get("label") or DEFAULT_LABEL),
            address=str(raw.get("address") or ""),
            transport=str(raw.get("transport") or "auto"),
            finite=bool(raw.get("finite")),
            auto_label=bool(raw.get("auto_label", True)),
        )

    def copy(self) -> "SavedInstrument":
        return SavedInstrument.from_dict(self.to_dict())

    def display(self) -> str:
        """One line for the Instruments menu and the top-bar picker."""
        address = self.address or "no address"
        if self.label and self.label != address:
            return f"{self.label} — {address}"
        return address

    def summary(self) -> str:
        """The second line of a list row: address, transport, flag."""
        parts = [self.address or "no address", self.transport]
        if self.finite:
            parts.append("finite count")
        return " · ".join(parts)


class InstrumentLibrary:
    """The persisted list of instruments, and which one was last connected.

    Stored as one JSON string rather than a ``QSettings`` array so that adding
    a field to :class:`SavedInstrument` cannot leave half-written entries from
    an older version behind.  A settings value that will not parse is replaced
    by an empty library rather than crashing the application at startup —
    reported through :attr:`load_error` so it is not silent.
    """

    def __init__(self, settings: Optional[QSettings] = None) -> None:
        self.settings = settings if settings is not None else QSettings()
        self.entries: List[SavedInstrument] = []
        self.selected: int = -1
        self.load_error: str = ""

    # ------------------------------------------------------------- storage

    def load(self) -> None:
        """Read the library, saying so whenever anything was lost.

        This is the one file the user cannot rebuild from the instrument, so
        every path that ends with fewer entries than were stored sets
        :attr:`load_error` (ARCHITECTURE.md section 1's no-silent-failure rule).
        Two cases used to lose the whole list without a word: a value that had
        been written as a *string list* — ``type=str`` renders that as ``""``,
        so the parse was skipped entirely — and a JSON list whose items were
        not objects, which the ``isinstance`` filter dropped one by one.
        """
        self.entries = []
        self.selected = -1
        self.load_error = ""
        stored_raw = self.settings.value(SETTINGS_LIST, None)
        raw = ""
        if isinstance(stored_raw, str):
            raw = stored_raw
        elif isinstance(stored_raw, (bytes, bytearray)):
            raw = bytes(stored_raw).decode("utf-8", "replace")
        elif isinstance(stored_raw, (list, tuple)):
            # QSettings gives a stored string list back as a list, and a
            # one-element list is indistinguishable from a plain string on
            # some backends.  Join it and try to parse: that recovers the
            # common case where the JSON document itself contained a comma and
            # was split on the way in.
            raw = ",".join(str(item) for item in stored_raw)
            self.load_error = (
                "the saved instrument list was stored as a list of strings "
                "rather than one value; it was rejoined before reading"
            )
        elif stored_raw is not None:
            self.load_error = (
                f"the saved instrument list was stored as "
                f"{type(stored_raw).__name__}, which cannot be read; starting "
                f"with an empty list"
            )
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                self.load_error = (
                    f"the saved instrument list could not be read ({exc}); "
                    f"starting with an empty list"
                )
                parsed = []
            if isinstance(parsed, list):
                dropped = 0
                for item in parsed:
                    if isinstance(item, dict):
                        self.entries.append(SavedInstrument.from_dict(item))
                    else:
                        dropped += 1
                if dropped:
                    self.load_error = (
                        f"{dropped} of the {len(parsed)} saved instrument "
                        f"entries were not readable and were dropped; the "
                        f"rest of the list was kept"
                    )
            else:
                self.load_error = (
                    "the saved instrument list was not a list; starting with "
                    "an empty list"
                )
        try:
            stored = self.settings.value(SETTINGS_SELECTED, -1, type=int)
        except (TypeError, ValueError):
            # A value that will not convert is a corrupt selection, not a
            # reason to lose the list that was just read.
            stored = -1
        if isinstance(stored, int) and 0 <= stored < len(self.entries):
            self.selected = stored
        elif self.entries:
            self.selected = 0

    def save(self) -> None:
        payload = json.dumps([entry.to_dict() for entry in self.entries])
        self.settings.setValue(SETTINGS_LIST, payload)
        self.settings.setValue(SETTINGS_SELECTED, self.selected)
        self.settings.sync()

    # -------------------------------------------------------------- access

    def __len__(self) -> int:
        return len(self.entries)

    def current(self) -> Optional[SavedInstrument]:
        if 0 <= self.selected < len(self.entries):
            return self.entries[self.selected]
        return None

    def index_of(self, entry: SavedInstrument) -> int:
        for index, candidate in enumerate(self.entries):
            if candidate is entry:
                return index
        return -1

    def find(self, address: str, transport: str = "") -> int:
        """The first entry with this address (and transport, if given)."""
        wanted = (address or "").strip().lower()
        for index, entry in enumerate(self.entries):
            if entry.address.strip().lower() != wanted:
                continue
            if transport and entry.transport != transport:
                continue
            return index
        return -1

    def add(self, entry: SavedInstrument) -> int:
        self.entries.append(entry)
        return len(self.entries) - 1

    def remove(self, index: int) -> None:
        if not 0 <= index < len(self.entries):
            return
        del self.entries[index]
        if self.selected >= len(self.entries):
            self.selected = len(self.entries) - 1


# ------------------------------------------------------------------- dialog


def _caption(text: str) -> QLabel:
    """The small mono caption every control in this application carries."""
    label = QLabel(text)
    label.setObjectName("Caption")
    label.setWordWrap(True)
    return label


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionLabel")
    return label


class ConnectDialog(QDialog):
    """Pick a saved instrument, edit the library, and connect.

    The dialog edits :class:`InstrumentLibrary` in place and saves it on either
    exit path: *Cancel* cancels connecting, not the list management the user
    just did.  Only *Connect* returns an entry to act on.
    """

    connectRequested = Signal(int)  # index into the library

    def __init__(
        self,
        library: InstrumentLibrary,
        parent: Optional[QWidget] = None,
        connected_index: int = -1,
    ) -> None:
        super().__init__(parent)
        self.library = library
        self._connected_index = connected_index
        self._loading = False
        self._chosen = -1

        self.setWindowTitle("Connect to an instrument")
        self.setModal(True)
        self.setMinimumSize(720, 440)
        self.resize(880, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(16, 14, 16, 10)
        body.setSpacing(16)
        body.addWidget(self._build_list(), 0)
        body.addWidget(self._build_form(), 1)
        outer.addLayout(body, 1)
        outer.addWidget(self._build_footer())

        if not library.entries:
            # First run: give the user a form to type into rather than an
            # empty list and three disabled boxes.  An entry that is still
            # empty when the dialog closes is dropped again.
            library.add(SavedInstrument())
            library.selected = 0
        self._reload_list()
        self._select_row(library.selected if library.selected >= 0 else 0)
        self.address_edit.setFocus()

    # -------------------------------------------------------------- building

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        # ARCHITECTURE.md section 5 restricts Martian Mono to the readout and
        # headline numerics; a modal dialog is the one place it rules out, so
        # this mark is set in IBM Plex Mono like every other caption-weight
        # identifier in the application.  It keeps the phosphor colour, which
        # is what makes it read as the instrument's own name.
        mark = QLabel("34461A")
        mark.setFont(theme.mono(14, QFont.DemiBold, 0.4))
        mark.setStyleSheet(f"color: {theme.PHOSPHOR};")
        layout.addWidget(mark)

        title = QLabel("Connect to an instrument")
        title.setFont(theme.sans(13, QFont.DemiBold))
        title.setStyleSheet(f"color: {theme.TEXT};")
        layout.addWidget(title)

        layout.addStretch(1)
        note = _caption("SCPI over LAN · one link per instrument")
        # It sits after a stretch, which would otherwise leave a wrapping
        # label at its minimum width and break the line in half.
        note.setWordWrap(False)
        layout.addWidget(note)
        return bar

    def _build_list(self) -> QWidget:
        holder = QWidget()
        holder.setFixedWidth(266)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(_section("SAVED INSTRUMENTS"))

        self.list = QListWidget()
        self.list.setObjectName("InstrumentList")
        self.list.setUniformItemSizes(False)
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemDoubleClicked.connect(lambda _item: self._connect())
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.add_button = QPushButton("Add")
        self.add_button.setToolTip("Add a new, empty instrument entry")
        self.add_button.clicked.connect(self._add)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.setToolTip(
            "Copy the selected entry — useful for the same meter on a second "
            "transport"
        )
        self.duplicate_button.clicked.connect(self._duplicate)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Delete the selected entry from the list")
        self.remove_button.clicked.connect(self._remove)
        for button in (self.add_button, self.duplicate_button, self.remove_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        return holder

    def _build_form(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)

        row = 0
        for text in ("NAME", "ADDRESS", "TRANSPORT"):
            name = QLabel(text)
            name.setObjectName("SectionLabel")
            grid.addWidget(name, row, 0, Qt.AlignRight | Qt.AlignVCenter)
            row += 2

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText(DEFAULT_LABEL)
        self.label_edit.setMaxLength(64)
        self.label_edit.textEdited.connect(self._label_edited)
        grid.addWidget(self.label_edit, 0, 1)
        grid.addWidget(_caption("from *IDN? unless you type your own"), 1, 1)

        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText(f"hostname or IP, e.g. {EXAMPLE_ADDRESS}")
        self.address_edit.setMaxLength(255)
        self.address_edit.textEdited.connect(self._address_edited)
        self.address_edit.returnPressed.connect(self._connect)
        grid.addWidget(self.address_edit, 2, 1)
        grid.addWidget(_caption("--instrument"), 3, 1)

        self.transport_box = QComboBox()
        # The descriptions are long on purpose — the difference between these
        # is whether the instrument recovers on its own from a crash — but the
        # dialog must not be sized by the longest one.  The popup still shows
        # each in full.
        self.transport_box.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.transport_box.setMinimumContentsLength(26)
        for key, text in TRANSPORTS:
            self.transport_box.addItem(text, key)
        self.transport_box.currentIndexChanged.connect(self._transport_changed)
        grid.addWidget(self.transport_box, 4, 1)
        grid.addWidget(_caption("--transport"), 5, 1)
        outer.addLayout(grid)

        self.finite_box = QCheckBox("Force finite trigger count")
        self.finite_box.setToolTip(
            "Keep the idle keepalive's finite renewed trigger count even on a "
            "transport that already stops the acquisition when this "
            "application dies. It costs one blanked reading in twelve on the "
            "instrument's own front panel, so it is off by default."
        )
        self.finite_box.toggled.connect(self._finite_changed)
        outer.addWidget(self.finite_box)
        outer.addWidget(_caption("--finite-trigger-count"))

        outer.addStretch(1)

        hint = QLabel(
            "The application holds exactly one link to one instrument. "
            "Connecting to another hands the current one back first: its "
            "trigger setup is restored and SYST:LOC is the last command sent, "
            "so its front panel free-runs again."
        )
        hint.setWordWrap(True)
        # A word-wrapped QLabel otherwise asks the layout for its full
        # single-line width, which would set the width of the whole dialog.
        hint.setMaximumWidth(520)
        hint.setStyleSheet(f"color: {theme.DIM};")
        outer.addWidget(hint)

        wrapper = QWidget()
        wrap_layout = QVBoxLayout(wrapper)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(6)
        wrap_layout.addWidget(_section("DETAILS"))
        wrap_layout.addWidget(panel, 1)
        return wrapper

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("DialogFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(10)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme.FAIL};")
        layout.addWidget(self.error_label, 1)

        cancel = QPushButton("Cancel")
        cancel.setToolTip(
            "Close without connecting. Changes to the saved list are kept."
        )
        cancel.clicked.connect(self._cancel)
        layout.addWidget(cancel)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("Primary")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self._connect)
        layout.addWidget(self.connect_button)
        return footer

    # ------------------------------------------------------------- list model

    def _reload_list(self) -> None:
        self._loading = True
        current = self.list.currentRow()
        self.list.clear()
        for index, entry in enumerate(self.library.entries):
            item = QListWidgetItem(f"{entry.label}\n{entry.summary()}")
            if index == self._connected_index:
                item.setText(f"{entry.label}   ● connected\n{entry.summary()}")
            self.list.addItem(item)
        self._loading = False
        if 0 <= current < self.list.count():
            self.list.setCurrentRow(current)
        self._update_buttons()

    def _refresh_row(self, index: int) -> None:
        item = self.list.item(index)
        if item is None:
            return
        entry = self.library.entries[index]
        mark = "   ● connected" if index == self._connected_index else ""
        item.setText(f"{entry.label}{mark}\n{entry.summary()}")

    def _select_row(self, index: int) -> None:
        if not self.library.entries:
            self._load_fields(None)
            self._update_buttons()
            return
        index = max(0, min(index, len(self.library.entries) - 1))
        self.list.setCurrentRow(index)

    def _row_changed(self, index: int) -> None:
        if self._loading:
            return
        if 0 <= index < len(self.library.entries):
            self.library.selected = index
            self._load_fields(self.library.entries[index])
        else:
            self._load_fields(None)
        self._update_buttons()

    def _load_fields(self, entry: Optional[SavedInstrument]) -> None:
        self._loading = True
        enabled = entry is not None
        for widget in (
            self.label_edit,
            self.address_edit,
            self.transport_box,
            self.finite_box,
        ):
            widget.setEnabled(enabled)
        if entry is None:
            self.label_edit.setText("")
            self.address_edit.setText("")
            self.transport_box.setCurrentIndex(0)
            self.finite_box.setChecked(False)
        else:
            self.label_edit.setText(entry.label)
            self.address_edit.setText(entry.address)
            position = self.transport_box.findData(entry.transport)
            self.transport_box.setCurrentIndex(position if position >= 0 else 0)
            self.finite_box.setChecked(entry.finite)
        self._loading = False
        self.error_label.setText("")

    def _update_buttons(self) -> None:
        entry = self._current_entry()
        has = entry is not None
        self.duplicate_button.setEnabled(has)
        self.remove_button.setEnabled(has)
        self.connect_button.setEnabled(has)

    def _current_entry(self) -> Optional[SavedInstrument]:
        index = self.list.currentRow()
        if 0 <= index < len(self.library.entries):
            return self.library.entries[index]
        return None

    # ----------------------------------------------------------- field edits

    def _label_edited(self, text: str) -> None:
        entry = self._current_entry()
        if entry is None or self._loading:
            return
        entry.label = text.strip() or DEFAULT_LABEL
        # The user has named this one; *IDN? must never overwrite it again.
        entry.auto_label = False
        self._refresh_row(self.list.currentRow())

    def _address_edited(self, text: str) -> None:
        entry = self._current_entry()
        if entry is None or self._loading:
            return
        entry.address = text.strip()
        if entry.auto_label:
            entry.label = DEFAULT_LABEL
        self._refresh_row(self.list.currentRow())
        self.error_label.setText("")

    def _transport_changed(self, index: int) -> None:
        entry = self._current_entry()
        if entry is None or self._loading:
            return
        entry.transport = str(self.transport_box.itemData(index) or "auto")
        self._refresh_row(self.list.currentRow())

    def _finite_changed(self, checked: bool) -> None:
        entry = self._current_entry()
        if entry is None or self._loading:
            return
        entry.finite = bool(checked)
        self._refresh_row(self.list.currentRow())

    # -------------------------------------------------------- list management

    def _add(self) -> None:
        index = self.library.add(SavedInstrument())
        self._reload_list()
        self.list.setCurrentRow(index)
        self.address_edit.setFocus()

    def _duplicate(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        clone = entry.copy()
        if not clone.auto_label:
            clone.label = f"{clone.label} (copy)"
        index = self.library.add(clone)
        self._reload_list()
        self.list.setCurrentRow(index)

    def _remove(self) -> None:
        index = self.list.currentRow()
        if not 0 <= index < len(self.library.entries):
            return
        if index == self._connected_index:
            self._connected_index = -1
        elif index < self._connected_index:
            self._connected_index -= 1
        self.library.remove(index)
        self._reload_list()
        self._select_row(min(index, len(self.library.entries) - 1))

    # ------------------------------------------------------------- finishing

    def chosen_index(self) -> int:
        """The library index the user pressed Connect on, or -1."""
        return self._chosen

    def _prune_empty(self) -> None:
        """Drop entries that were never given an address.

        The dialog adds a blank entry to type into on a first run and whenever
        Add is pressed; one left untouched should not become a permanent
        "no address" row in the Instruments menu.

        The selection and the connected mark are re-found **by identity**
        afterwards, not clamped.  Clamping quietly moved them: ``[blank, A, B]``
        with A selected became ``[A, B]`` with **B** selected, and the same
        shift would have pointed the "connected" mark at the wrong meter.
        """
        entries = self.library.entries
        selected = self.library.current()
        connected = (
            entries[self._connected_index]
            if 0 <= self._connected_index < len(entries)
            else None
        )
        keep = [entry for entry in entries if entry.address.strip()]
        if len(keep) == len(entries):
            return
        self.library.entries = keep
        self.library.selected = (
            self.library.index_of(selected) if selected is not None else -1
        )
        if self.library.selected < 0 and keep:
            self.library.selected = 0
        self._connected_index = (
            self.library.index_of(connected) if connected is not None else -1
        )

    def _connect(self) -> None:
        entry = self._current_entry()
        if entry is None:
            self.error_label.setText("Add an instrument first.")
            return
        problem = validate_address(entry.address)
        if problem:
            self.error_label.setText(problem)
            self.address_edit.setFocus()
            self.address_edit.selectAll()
            return
        self._prune_empty()
        self._chosen = self.library.index_of(entry)
        self.library.selected = self._chosen
        self.library.save()
        self.accept()

    def _cancel(self) -> None:
        self.reject()

    def reject(self) -> None:  # noqa: N802 - Qt override; Esc arrives here too
        """Cancel connecting.  The list edits stand and are saved anyway."""
        self._prune_empty()
        self.library.save()
        self._chosen = -1
        super().reject()
