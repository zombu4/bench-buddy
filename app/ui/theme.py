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

"""Design tokens, bundled fonts and the application stylesheet.

The palette is SPEC.md section 6 verbatim: a cool blue-black instrument bezel
carrying a warm phosphor readout.  Nothing here reads the host theme — the
window looks the same on Windows, macOS and Debian, in light mode or dark.

Fonts ship inside the application (``app/ui/fonts``) and are registered with
``QFontDatabase.addApplicationFont``.  No system font is ever assumed present.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# --------------------------------------------------------------------- tokens

INK = "#0E1419"
PANEL = "#151D25"
PANEL2 = "#1C2731"
RULE = "#293742"
TEXT = "#C4D2DD"
DIM = "#6B7C8C"
PHOSPHOR = "#F2EFE6"
SIGNAL = "#4FC3E8"
WARN = "#E8A33D"
FAIL = "#E5604D"
OK = "#6FCF7F"

# Derived, kept here so widgets never invent a colour of their own.
GLASS = "#0A0F13"  # the recessed display well behind the digits
GLASS_EDGE = "#1F2C36"
PHOSPHOR_DIM = "#F2EFE6"  # same hue; the widget applies the 35% alpha
SIGNAL_MUTED = "#2A4E5E"

C: Dict[str, QColor] = {
    "ink": QColor(INK),
    "panel": QColor(PANEL),
    "panel2": QColor(PANEL2),
    "rule": QColor(RULE),
    "text": QColor(TEXT),
    "dim": QColor(DIM),
    "phosphor": QColor(PHOSPHOR),
    "signal": QColor(SIGNAL),
    "warn": QColor(WARN),
    "fail": QColor(FAIL),
    "ok": QColor(OK),
    "glass": QColor(GLASS),
    "glass_edge": QColor(GLASS_EDGE),
}

# ---------------------------------------------------------------------- fonts

FONT_FILES = (
    "MartianMono-Regular.ttf",
    "IBMPlexSans-Regular.ttf",
    "IBMPlexMono-Regular.ttf",
    "IBMPlexMono-Medium.ttf",
)

# Filled in by load_fonts(); the family names the bundled files register under.
FAMILY_READOUT = "Martian Mono"
FAMILY_SANS = "IBM Plex Sans"
FAMILY_MONO = "IBM Plex Mono"


class FontError(RuntimeError):
    """A bundled font could not be found or registered with Qt."""


def resource_dir() -> str:
    """The directory holding this package's data files.

    Works from source and from a PyInstaller one-folder bundle, where the
    payload is unpacked next to the executable (``sys._MEIPASS``).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = os.path.join(meipass, "app", "ui")
        if os.path.isdir(bundled):
            return bundled
        return meipass
    return os.path.dirname(os.path.abspath(__file__))


def font_dir() -> str:
    return os.path.join(resource_dir(), "fonts")


def load_fonts() -> List[str]:
    """Register every bundled face; return the families Qt now knows.

    Raises :class:`FontError` rather than falling back to a system font: a
    silently substituted face would break the fixed digit geometry that the
    readout depends on.
    """
    directory = font_dir()
    families: List[str] = []
    for name in FONT_FILES:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            raise FontError(f"bundled font missing: {path}")
        font_id = QFontDatabase.addApplicationFont(path)
        if font_id < 0:
            raise FontError(f"Qt refused to load the bundled font {path}")
        for family in QFontDatabase.applicationFontFamilies(font_id):
            if family not in families:
                families.append(family)
    for required in (FAMILY_READOUT, FAMILY_SANS, FAMILY_MONO):
        if required not in families:
            raise FontError(
                f"{required!r} did not register; Qt reported {families}"
            )
    return families


def _font(family: str, size: int, weight: QFont.Weight, spacing: float = 0.0) -> QFont:
    font = QFont(family)
    font.setPixelSize(size)
    font.setWeight(weight)
    font.setStyleStrategy(QFont.PreferAntialias)
    # Every bundled face is tabular, but say so explicitly: a digit must never
    # change advance width between values or between weights.
    font.setStyleHint(QFont.StyleHint.AnyStyle, QFont.StyleStrategy.PreferMatch)
    if spacing:
        font.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return font


def sans(size: int = 13, weight: QFont.Weight = QFont.Normal, spacing: float = 0.0) -> QFont:
    return _font(FAMILY_SANS, size, weight, spacing)


def mono(size: int = 12, weight: QFont.Weight = QFont.Normal, spacing: float = 0.0) -> QFont:
    return _font(FAMILY_MONO, size, weight, spacing)


def readout(size: int, weight: QFont.Weight = QFont.Normal, spacing: float = 0.0) -> QFont:
    return _font(FAMILY_READOUT, size, weight, spacing)


def caption() -> QFont:
    """The SCPI-node caption that sits under every control."""
    return mono(10, QFont.Normal, 0.2)


def label() -> QFont:
    """The small upper-case label above a control."""
    return sans(10, QFont.DemiBold, 0.9)


# ------------------------------------------------------------------- palette


def palette() -> QPalette:
    """An explicit palette; nothing is inherited from the desktop."""
    p = QPalette()
    p.setColor(QPalette.Window, C["ink"])
    p.setColor(QPalette.WindowText, C["text"])
    p.setColor(QPalette.Base, C["panel"])
    p.setColor(QPalette.AlternateBase, C["panel2"])
    p.setColor(QPalette.Text, C["text"])
    p.setColor(QPalette.Button, C["panel2"])
    p.setColor(QPalette.ButtonText, C["text"])
    p.setColor(QPalette.BrightText, C["phosphor"])
    p.setColor(QPalette.Highlight, C["signal"])
    p.setColor(QPalette.HighlightedText, C["ink"])
    p.setColor(QPalette.ToolTipBase, C["panel2"])
    p.setColor(QPalette.ToolTipText, C["text"])
    p.setColor(QPalette.PlaceholderText, C["dim"])
    p.setColor(QPalette.Link, C["signal"])
    p.setColor(QPalette.LinkVisited, C["signal"])
    p.setColor(QPalette.Light, C["rule"])
    p.setColor(QPalette.Midlight, C["rule"])
    p.setColor(QPalette.Mid, C["rule"])
    p.setColor(QPalette.Dark, C["ink"])
    p.setColor(QPalette.Shadow, QColor(0, 0, 0))
    disabled = QColor(DIM)
    disabled.setAlpha(120)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    return p


# ---------------------------------------------------------------- stylesheet

STYLESHEET = f"""
* {{
    outline: none;
}}

QWidget {{
    background: {INK};
    color: {TEXT};
    font-family: "{FAMILY_SANS}";
    font-size: 13px;
}}

QLabel {{
    background: transparent;
}}

QCheckBox {{
    background: transparent;
    spacing: 6px;
}}
/* Fusion draws only a bare checkmark against this palette, so an unchecked box
   is an invisible control — the user cannot see there is anything to click.
   A drawn well, filled with the accent when set, reads in both states. */
QCheckBox::indicator {{
    width: 13px;
    height: 13px;
    border: 1px solid {RULE};
    border-radius: 2px;
    background: {PANEL2};
}}
QCheckBox::indicator:hover {{
    border-color: #354856;
}}
QCheckBox::indicator:checked {{
    background: {SIGNAL};
    border-color: {SIGNAL};
}}
QCheckBox::indicator:disabled {{
    background: #141C23;
    border-color: #222E38;
}}
QCheckBox::indicator:checked:disabled {{
    background: #2E3E4B;
    border-color: #2E3E4B;
}}

QToolTip {{
    background: {PANEL2};
    color: {TEXT};
    border: 1px solid {RULE};
    padding: 4px 7px;
    font-family: "{FAMILY_SANS}";
}}

/* ---------------------------------------------------------------- panels */

QFrame#Panel {{
    background: {PANEL};
    border: 1px solid {RULE};
    border-radius: 3px;
}}

QFrame#Bezel {{
    background: {PANEL};
    border: 1px solid {RULE};
    border-radius: 4px;
}}

QFrame#TopBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {RULE};
}}

QFrame#Rail {{
    background: {PANEL};
    border: none;
    border-right: 1px solid {RULE};
}}

QFrame#RightRail {{
    background: {INK};
    border: none;
    border-left: 1px solid {RULE};
}}

QFrame#HRule {{
    background: {RULE};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}

QLabel#SectionLabel {{
    color: {DIM};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1.1px;
    text-transform: uppercase;
}}

QLabel#Caption {{
    color: {DIM};
    font-family: "{FAMILY_MONO}";
    font-size: 10px;
    letter-spacing: 0.2px;
}}

QLabel#Value {{
    color: {TEXT};
    font-family: "{FAMILY_MONO}";
    font-size: 12px;
}}

QLabel#ValueBig {{
    color: {PHOSPHOR};
    font-family: "{FAMILY_MONO}";
    font-size: 15px;
}}

QLabel#Ident {{
    color: {TEXT};
    font-family: "{FAMILY_MONO}";
    font-size: 12px;
}}

QLabel#IdentKey {{
    color: {DIM};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
}}

/* --------------------------------------------------------------- buttons */

QPushButton {{
    background: {PANEL2};
    color: {TEXT};
    border: 1px solid {RULE};
    border-radius: 3px;
    padding: 5px 12px;
    font-size: 12px;
}}
QPushButton:hover {{
    background: #24313C;
    border-color: #354856;
}}
QPushButton:pressed {{
    background: #10181F;
}}
QPushButton:focus {{
    border: 1px solid {SIGNAL};
}}
QPushButton:disabled {{
    color: #4E5C69;
    background: #141C23;
    border-color: #222E38;
}}
QPushButton:checked {{
    background: {SIGNAL_MUTED};
    border-color: {SIGNAL};
    color: {PHOSPHOR};
}}

QPushButton#Primary {{
    background: {SIGNAL_MUTED};
    border-color: {SIGNAL};
    color: {PHOSPHOR};
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: #33637A;
}}

QPushButton#RunButton {{
    background: {SIGNAL_MUTED};
    border: 1px solid {SIGNAL};
    color: {PHOSPHOR};
    font-weight: 600;
    padding: 6px 20px;
}}
QPushButton#RunButton:hover {{
    background: #33637A;
}}
QPushButton#RunButton[running="true"] {{
    background: #5C2A24;
    border-color: {FAIL};
    color: #FFD9D2;
}}
QPushButton#RunButton[running="true"]:hover {{
    background: #74352D;
}}
/* Without this the run button keeps its lit background while disabled, which
   now happens for a whole state rather than an instant: the application can
   sit disconnected, with no instrument to run. */
QPushButton#RunButton:disabled {{
    background: #141C23;
    border-color: #222E38;
    color: #4E5C69;
}}

/* the rotary-switch equivalent */
QPushButton#FuncButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 2px;
    padding: 5px 8px 5px 10px;
    text-align: left;
    color: {TEXT};
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
QPushButton#FuncButton:hover {{
    background: {PANEL2};
}}
QPushButton#FuncButton:focus {{
    border: 1px solid {SIGNAL};
}}
QPushButton#FuncButton:checked {{
    background: {PANEL2};
    border-left: 2px solid {SIGNAL};
    color: {PHOSPHOR};
}}

QPushButton#Toggle {{
    padding: 4px 10px;
    font-size: 12px;
}}
QPushButton#Toggle:checked {{
    background: {SIGNAL_MUTED};
    border-color: {SIGNAL};
    color: {PHOSPHOR};
}}

QPushButton#Seg {{
    padding: 4px 9px;
    font-size: 11px;
    font-family: "{FAMILY_MONO}";
}}

/* ---------------------------------------------------------------- inputs */

QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {PANEL2};
    color: {TEXT};
    border: 1px solid {RULE};
    border-radius: 3px;
    padding: 4px 8px;
    font-family: "{FAMILY_MONO}";
    font-size: 12px;
    selection-background-color: {SIGNAL};
    selection-color: {INK};
}}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {SIGNAL};
}}
QComboBox:disabled, QLineEdit:disabled {{
    color: #4E5C69;
    background: #141C23;
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {DIM};
    width: 0;
    height: 0;
    margin-right: 7px;
}}
QComboBox QAbstractItemView {{
    background: {PANEL2};
    color: {TEXT};
    border: 1px solid {RULE};
    selection-background-color: {SIGNAL_MUTED};
    selection-color: {PHOSPHOR};
    font-family: "{FAMILY_MONO}";
    outline: none;
}}

/* ------------------------------------------------------------------ tabs */

QTabWidget::pane {{
    background: {PANEL};
    border: 1px solid {RULE};
    border-radius: 3px;
    top: -1px;
}}
QTabBar {{
    background: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: {DIM};
    border: 1px solid transparent;
    border-bottom: 1px solid {RULE};
    padding: 6px 16px;
    margin-right: 2px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
QTabBar::tab:hover {{
    color: {TEXT};
}}
QTabBar::tab:selected {{
    background: {PANEL};
    color: {PHOSPHOR};
    border: 1px solid {RULE};
    border-bottom: 1px solid {PANEL};
}}
QTabBar::tab:focus {{
    border: 1px solid {SIGNAL};
}}

/* --------------------------------------------------------------- tables */

QTableView {{
    background: {PANEL};
    alternate-background-color: #18212A;
    color: {TEXT};
    border: 1px solid {RULE};
    gridline-color: #1E2A34;
    font-family: "{FAMILY_MONO}";
    font-size: 12px;
    selection-background-color: {SIGNAL_MUTED};
    selection-color: {PHOSPHOR};
}}
QHeaderView::section {{
    background: {PANEL2};
    color: {DIM};
    border: none;
    border-right: 1px solid {RULE};
    border-bottom: 1px solid {RULE};
    padding: 4px 8px;
    font-family: "{FAMILY_SANS}";
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.8px;
}}
QTableCornerButton::section {{
    background: {PANEL2};
    border: none;
}}

/* --------------------------------------------------------- text surfaces */

QPlainTextEdit, QTextEdit {{
    background: {GLASS};
    color: {TEXT};
    border: 1px solid {RULE};
    border-radius: 3px;
    font-family: "{FAMILY_MONO}";
    font-size: 12px;
    selection-background-color: {SIGNAL_MUTED};
    selection-color: {PHOSPHOR};
}}

/* -------------------------------------------------------------- scrolling */

QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #2E3E4B;
    min-height: 28px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: #3C5060;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #2E3E4B;
    min-width: 28px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #3C5060;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
    background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}

/* --------------------------------------------------------------- splitter */

QSplitter::handle {{
    background: {RULE};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}

/* ----------------------------------------------------------------- menus */

QMenuBar {{
    background: {PANEL};
    color: {TEXT};
    border: none;
    border-bottom: 1px solid {RULE};
    padding: 1px 6px;
}}
QMenuBar::item {{
    background: transparent;
    color: {TEXT};
    padding: 5px 10px;
    border-radius: 2px;
    font-size: 12px;
}}
QMenuBar::item:selected {{
    background: {PANEL2};
    color: {PHOSPHOR};
}}
QMenuBar::item:pressed {{
    background: {SIGNAL_MUTED};
    color: {PHOSPHOR};
}}

QMenu {{
    background: {PANEL2};
    color: {TEXT};
    border: 1px solid {RULE};
    padding: 4px 0;
}}
QMenu::item {{
    padding: 5px 22px 5px 24px;
    font-size: 12px;
}}
QMenu::item:selected {{
    background: {SIGNAL_MUTED};
    color: {PHOSPHOR};
}}
QMenu::item:disabled {{
    color: #4E5C69;
}}
QMenu::separator {{
    height: 1px;
    background: {RULE};
    margin: 4px 10px;
}}

/* --------------------------------------------------------------- dialogs */

QDialog {{
    background: {INK};
}}

QFrame#DialogFooter {{
    background: {PANEL};
    border: none;
    border-top: 1px solid {RULE};
}}

QListWidget#InstrumentList {{
    background: {PANEL};
    border: 1px solid {RULE};
    border-radius: 3px;
    outline: none;
    font-family: "{FAMILY_SANS}";
    font-size: 12px;
}}
QListWidget#InstrumentList::item {{
    color: {TEXT};
    border-bottom: 1px solid #1E2A34;
    padding: 7px 10px;
}}
QListWidget#InstrumentList::item:hover {{
    background: {PANEL2};
}}
QListWidget#InstrumentList::item:selected {{
    background: {SIGNAL_MUTED};
    color: {PHOSPHOR};
    border-left: 2px solid {SIGNAL};
}}

/* ---------------------------------------------- the top-bar link control */

QComboBox#InstrumentPicker {{
    font-family: "{FAMILY_SANS}";
    font-size: 12px;
    padding: 4px 6px;
}}

QPushButton#LinkButton {{
    min-width: 104px;
    font-weight: 600;
}}
QPushButton#LinkButton[phase="offline"] {{
    background: {SIGNAL_MUTED};
    border-color: {SIGNAL};
    color: {PHOSPHOR};
}}
QPushButton#LinkButton[phase="offline"]:hover {{
    background: #33637A;
}}
QPushButton#LinkButton[phase="connected"] {{
    background: {PANEL2};
    border-color: {RULE};
    color: {TEXT};
}}
QPushButton#LinkButton[phase="connected"]:hover {{
    background: #5C2A24;
    border-color: {FAIL};
    color: #FFD9D2;
}}
QPushButton#LinkButton[phase="connecting"],
QPushButton#LinkButton[phase="disconnecting"] {{
    background: #141C23;
    border-color: {WARN};
    color: {WARN};
}}

QMessageBox {{
    background: {PANEL};
}}
QMessageBox QLabel {{
    color: {TEXT};
}}
"""


def apply(app) -> List[str]:
    """Install fonts, palette and stylesheet on *app*; return font families."""
    families = load_fonts()
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setStyleSheet(STYLESHEET)
    base = sans(13)
    app.setFont(base)
    app.setAttribute(Qt.AA_DontShowIconsInMenus, False)
    return families
