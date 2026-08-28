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

"""The resolution-aware readout — the element this application is remembered by.

Digit slots come from the *configuration* (range, resolution, unit) and never
from the live value, so no digit ever changes width or position while the
number updates.  Digits at or above ``<func>:RES?`` are solid warm phosphor;
digits below it are dimmed to 35%, with exactly one guard digit shown so the
resolution boundary is legible.  Beneath the number sits the resolution band.

When the instrument reports no resolution — CAP, FREQ, PER, TEMP, CONT, DIOD —
every digit is solid, the band is hidden, and the caption names why.  No
accuracy or uncertainty figure is ever synthesised: the only numbers here are
ones the instrument reported.

Ported from ``reference/web/app.js`` (``computeLayout``/``buildSlots``/
``renderValue``/``renderBand``) with the corrections noted inline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme
from .units import (
    MINUS,
    RANGE_IS_READING_UNIT,
    base_unit,
    effective_resolution,
    fmt_si,
    fmt_state,
    is_num,
    max_exponent,
    node_for,
    scale_unit,
    unit_no_prefix,
    unit_text,
)

# The fixed field a dB/dBm reading is drawn in.  Seven digits, the same total
# as every other function, and wide enough for anything this instrument can
# produce — see the ``scaled`` branch of compute_layout for why it is fixed.
SCALED_INT_DIGITS = 3
SCALED_DECIMALS = 4

# The tallest the no-band caption's strip may grow before it starts eating the
# glass.  Three wrapped lines of the caption face.
MAX_CAPTION_HEIGHT = 58.0

DIM_ALPHA = 0.35  # "dimmed to about 35% opacity" — ARCHITECTURE.md section 3
BLANK_ALPHA = 0.20  # the "no reading yet" placeholder, quieter than a digit
MAX_DIGIT_PX = 84
MIN_DIGIT_PX = 22

# Field geometry, in multiples of one digit advance.  Every glyph in this face
# is monospaced, so the digit advance is the unit the whole field is built on.
POINT_CELL = 0.60  # the cell holding the decimal point
THIN_CELL = 0.34  # thin space between decimal triples
UNIT_GAP = 0.50  # air between the last digit cell and the unit
UNIT_SHARE = 0.46  # the unit is drawn at 46% of the digit size
DOT_CENTRE = 0.46  # the point's visible centre inside its cell: slightly
# left of middle, which leaves marginally more air on the right and is what
# reads correctly for a decimal point.
SI_BY_EXP = {
    9: "G",
    6: "M",
    3: "k",
    0: "",
    -3: "m",
    -6: "µ",
    -9: "n",
    -12: "p",
    -15: "f",
}


class Geometry(NamedTuple):
    """Measured layout of the number field at one digit pixel size."""

    size: int
    advance: float
    point_cell: float
    thin_cell: float
    dot_offset: float  # add to the point cell origin to draw "."
    unit_font: QFont
    unit_width: float
    field_width: float  # sign + digits + separators
    total_width: float  # field + gap + unit
    cap_height: float


@dataclass(frozen=True)
class Slot:
    """One fixed digit cell."""

    place: float  # 10**n, the cell's place value in scaled units
    dim: bool  # below the reported resolution


@dataclass(frozen=True)
class Layout:
    mult: float
    prefix: str
    no_prefix: bool
    int_digits: int
    decimals: int
    res_scaled: Optional[float]
    unit: str
    func: str
    slots: Tuple[Slot, ...]

    @property
    def unit_label(self) -> str:
        return ("" if self.no_prefix else self.prefix) + self.unit

    def key(self) -> Tuple:
        return (
            self.mult,
            self.int_digits,
            self.decimals,
            self.res_scaled,
            self.unit_label,
            self.func,
        )


def compute_layout(
    state: Dict[str, Any], magnitude_hint: Optional[float] = None
) -> Layout:
    """Digit geometry from configuration alone.

    The magnitude hint is consulted only for the functions whose range node is
    in another unit or absent (FREQ, PER, TEMP, CONT, DIOD) — there is no
    configured span to size those from.  For every function that has a range
    in its own unit the geometry is fixed by the range and never moves.

    A dB/dBm reading has no configured span either, but unlike FREQ it has a
    known bounded magnitude, so it is given a fixed seven-digit field rather
    than one sized from the reading; see the ``scaled`` branch below.
    """
    no_prefix = unit_no_prefix(state)
    unit = unit_text(state)
    func = str(state.get("func") or "")
    range_value = state.get("range")
    # Withdrawn under dB/dBm scaling, where <p>:RES? is in the function's own
    # unit and says nothing about a logarithmic reading — see
    # units.effective_resolution.
    resolution = effective_resolution(state)
    # The range is in volts too, so it cannot size a dB field either; those
    # readings are sized from the value itself, like FREQ and TEMP.
    scaled = bool(scale_unit(state))

    if scaled:
        # DECIDED, not overlooked (REVIEW-CONNECT.md suspected finding B).
        #
        # Under dB/dBm there is genuinely no configured span to size the field
        # from: the range is in volts and says nothing about a logarithm of a
        # ratio.  Sizing from the reading instead — which is what the
        # magnitude hint does for FREQ, PER and TEMP — meant a reading walking
        # across a decade (-9.9 -> -10.1 dB) re-laid out the whole field,
        # and ARCHITECTURE.md section 3 says the geometry must never move while
        # the number updates.
        #
        # So a logarithmic reading gets a *fixed* geometry: three integer
        # digits and four decimals, seven digits in all, which is the same
        # total this readout gives every other function and covers
        # -999.9999 to +999.9999 dB — far beyond anything this instrument can
        # produce.  Nothing about the value moves it.
        #
        # The cost is honest and worth stating: close to 0 dB the old
        # behaviour showed six decimals and this shows four.  A dB reading is
        # a ratio, four decimals is 0.0001 dB, and no accuracy figure is
        # claimed for a scaled reading anyway (the band is withdrawn — see
        # _no_band_caption).  A field that stays still is worth more than two
        # digits that only appear when the reading happens to be small.  The
        # full-precision value is still in the chart crosshair, the log table
        # and the exported CSV.
        return Layout(
            mult=1.0,
            prefix="",
            no_prefix=True,
            int_digits=SCALED_INT_DIGITS,
            decimals=SCALED_DECIMALS,
            res_scaled=None,
            unit=unit,
            func=func,
            slots=_rebuild_slots(SCALED_INT_DIGITS, SCALED_DECIMALS, None),
        )

    if (
        is_num(range_value)
        and range_value != 0
        and func in RANGE_IS_READING_UNIT
    ):
        ref = abs(float(range_value))
    elif is_num(magnitude_hint) and magnitude_hint:
        ref = abs(float(magnitude_hint))
    elif is_num(resolution) and resolution > 0:
        # Six and a half digits above the resolution step.
        ref = abs(float(resolution)) * 1e6
    else:
        ref = 1.0
    if not math.isfinite(ref) or ref <= 0:
        ref = 1.0

    mult = 1.0
    prefix = ""
    if not no_prefix:
        exponent = math.floor(math.log10(ref) + 1e-9)
        group = int(math.floor(exponent / 3.0) * 3)
        # This meter reads to 1000 V and 3 A, so it never shows kV or kA:
        # 1000 V full scale belongs in the volts field, four digits wide.
        group_max = 0 if unit in ("V", "A") else 9
        group = max(-15, min(group_max, group))
        mult = 10.0 ** group
        prefix = SI_BY_EXP.get(group, "")

    ref_scaled = ref / mult
    int_digits = max(1, min(5, int(math.floor(math.log10(ref_scaled) + 1e-9)) + 1))

    res_scaled = (
        float(resolution) / mult if is_num(resolution) and resolution > 0 else None
    )
    if res_scaled is not None:
        # The last *solid* decimal is the smallest place whose value is still
        # at or above the reported resolution, then one guard digit below it.
        # The web reference rounded -log10(resolution), which is only right for
        # exact decades; this instrument answers <func>:RES? with values like
        # 3e-5, and rounding those produced two dimmed digits instead of one.
        solid = max(0, int(math.floor(-math.log10(res_scaled) + 1e-9)))
        decimals = min(9, solid + 1)
    else:
        decimals = max(0, min(8, 7 - int_digits))

    slots: List[Slot] = []
    for index in range(int_digits):
        place = 10.0 ** (int_digits - 1 - index)
        slots.append(Slot(place, False))
    for decimal in range(1, decimals + 1):
        place = 10.0 ** (-decimal)
        dim = res_scaled is not None and place < res_scaled * (1 - 1e-9)
        slots.append(Slot(place, dim))

    return Layout(
        mult=mult,
        prefix=prefix,
        no_prefix=no_prefix,
        int_digits=int_digits,
        decimals=decimals,
        res_scaled=res_scaled,
        unit=unit,
        func=func,
        slots=tuple(slots),
    )


class ReadoutWidget(QWidget):
    """The hero: identity strip, the digits on their glass, the resolution band."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(196)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

        self._state: Optional[Dict[str, Any]] = None
        self._layout: Optional[Layout] = None
        self._value: Optional[float] = None
        self._overload = False
        self._have_value = False
        self._flags: List[Tuple[str, str]] = []
        self._live = False

        self._font_cache: Dict[int, QFont] = {}
        self._geometry_cache: Dict[Tuple, Geometry] = {}

    # ----------------------------------------------------------------- input

    def set_state(self, state: Dict[str, Any]) -> None:
        previous = self._state
        self._state = state
        if previous is None or previous.get("func") != state.get("func"):
            self._value = None
            self._have_value = False
            self._overload = False
        self._relayout(force=True)
        # The full caption is always available, whatever the window size does
        # to the strip it is painted in.
        self.setToolTip("" if self._band_visible() else self._no_band_caption())
        self.update()

    def set_flags(self, flags: List[Tuple[str, str]]) -> None:
        if flags != self._flags:
            self._flags = list(flags)
            self.update()

    def set_live(self, live: bool) -> None:
        if live != self._live:
            self._live = live
            self.update()

    def set_value(self, value: Optional[float], overload: bool) -> None:
        """Record the newest sample.  Painting is driven by the 30 Hz timer."""
        self._value = value
        self._overload = overload
        self._have_value = True

    def clear_value(self) -> None:
        self._value = None
        self._overload = False
        self._have_value = False
        self.update()

    # ---------------------------------------------------------------- layout

    def _relayout(self, force: bool = False) -> None:
        if self._state is None:
            self._layout = None
            return
        layout = compute_layout(self._state, self._value)
        if force or self._layout is None or self._layout.key() != layout.key():
            self._layout = layout

    def _refit_if_needed(self) -> None:
        """Re-fit only the functions with no configured span to size from."""
        layout = self._layout
        if layout is None or self._state is None or layout.res_scaled is not None:
            return
        if self._state.get("func") in RANGE_IS_READING_UNIT:
            return
        value = self._value
        if not is_num(value) or value == 0:
            return
        scaled = abs(float(value) / layout.mult)
        if scaled >= 10.0 ** layout.int_digits or scaled < 0.1:
            self._layout = compute_layout(self._state, value)

    def _digit_font(self, pixels: int) -> QFont:
        font = self._font_cache.get(pixels)
        if font is None:
            font = theme.readout(pixels, QFont.Medium)
            self._font_cache[pixels] = font
        return font

    # ---------------------------------------------------------------- paint

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = QRectF(self.rect())
        self._paint_ground(painter, rect)

        header = QRectF(rect.left() + 16, rect.top() + 12, rect.width() - 32, 22)
        self._paint_header(painter, header)

        glass_top = header.bottom() + 10
        band_height = (
            46.0
            if self._band_visible()
            else self._caption_height(rect.width() - 32)
        )
        glass = QRectF(
            rect.left() + 12,
            glass_top,
            rect.width() - 24,
            max(60.0, rect.bottom() - 12 - band_height - glass_top),
        )
        self._paint_glass(painter, glass)
        self._paint_number(painter, glass)

        band = QRectF(
            rect.left() + 16,
            glass.bottom() + 6,
            rect.width() - 32,
            band_height - 6,
        )
        self._paint_band(painter, band)
        painter.end()

    def _band_visible(self) -> bool:
        return effective_resolution(self._state) is not None

    def _caption_height(self, width: float) -> float:
        """How tall the no-band caption's strip has to be to read in full.

        The caption is the sentence saying that **no accuracy figure is being
        shown**, which is the honesty this element exists to provide, and it
        was drawn with a single ``drawText`` into a fixed 24 px strip: at the
        shipped window size the dB caption — about 150 characters — was cut
        mid-sentence, at "…Every digit is solid and no".  It is word-wrapped
        now, and the strip is measured to fit rather than assumed.
        """
        text = self._no_band_caption()
        if not text:
            return 30.0
        metrics = QFontMetricsF(theme.caption())
        needed = metrics.boundingRect(
            QRectF(0, 0, max(80.0, width), 1000.0),
            int(Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap),
            text,
        ).height()
        # 6 px of air above the strip (see paintEvent) plus 4 below it.  The
        # cap keeps a pathologically narrow window from eating the glass; the
        # caption elides on that last line rather than being cut mid-word.
        return max(30.0, min(MAX_CAPTION_HEIGHT, needed + 10.0))

    def _paint_ground(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(theme.C["rule"], 1))
        painter.setBrush(theme.C["panel"])
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

    def _paint_header(self, painter: QPainter, rect: QRectF) -> None:
        state = self._state or {}
        painter.setFont(theme.readout(15, QFont.DemiBold))
        metrics = QFontMetricsF(painter.font())
        short = str(state.get("short") or "—")
        painter.setPen(theme.C["phosphor"])
        painter.drawText(
            QRectF(rect.left(), rect.top(), metrics.horizontalAdvance(short) + 4, rect.height()),
            int(Qt.AlignLeft | Qt.AlignVCenter),
            short,
        )
        cursor = rect.left() + metrics.horizontalAdvance(short) + 12

        painter.setFont(theme.sans(12))
        label_metrics = QFontMetricsF(painter.font())
        label = str(state.get("func_label") or "")
        painter.setPen(theme.C["text"])
        painter.drawText(
            QRectF(cursor, rect.top(), label_metrics.horizontalAdvance(label) + 4, rect.height()),
            int(Qt.AlignLeft | Qt.AlignVCenter),
            label,
        )
        cursor += label_metrics.horizontalAdvance(label) + 12

        node = "CONF:" + str(state.get("func") or "")
        painter.setFont(theme.caption())
        painter.setPen(theme.C["dim"])
        painter.drawText(
            QRectF(cursor, rect.top(), rect.right() - cursor, rect.height()),
            int(Qt.AlignLeft | Qt.AlignVCenter),
            node,
        )
        self._paint_flags(painter, rect)

    def _paint_flags(self, painter: QPainter, rect: QRectF) -> None:
        if not self._flags:
            return
        painter.setFont(theme.sans(10, QFont.DemiBold, 0.6))
        metrics = QFontMetricsF(painter.font())
        x = rect.right()
        for text, kind in reversed(self._flags):
            width = metrics.horizontalAdvance(text) + 14
            pill = QRectF(x - width, rect.top() + 2, width, rect.height() - 4)
            colour = {
                "run": theme.C["signal"],
                "on": theme.C["signal"],
                "warn": theme.C["warn"],
                "fail": theme.C["fail"],
                "ok": theme.C["ok"],
            }.get(kind, theme.C["dim"])
            fill = QColor(colour)
            fill.setAlpha(38)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(colour), 1))
            painter.drawRoundedRect(pill, 2, 2)
            painter.setPen(QColor(colour))
            painter.drawText(pill, int(Qt.AlignCenter), text)
            x -= width + 6

    def _paint_glass(self, painter: QPainter, rect: QRectF) -> None:
        """The recessed display well: cool bezel, deep ground, warm content."""
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.C["glass"])
        painter.drawRoundedRect(rect, 3, 3)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(theme.C["glass_edge"], 1))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        if self._live:
            glow = QColor(theme.SIGNAL)
            glow.setAlpha(46)
            painter.setPen(QPen(glow, 1))
            painter.drawLine(
                QPointF(rect.left() + 3, rect.top() + 0.5),
                QPointF(rect.right() - 3, rect.top() + 0.5),
            )

    # -------------------------------------------------------- the number

    def _geometry(self, layout: Layout, size: int) -> Geometry:
        """Every measurement the number needs, at one digit pixel size.

        Widths are measured, never estimated: the fit, the centring and the
        painting all read the same numbers, so the field cannot be sized to
        one width and drawn at another.
        """
        key = (size, layout.int_digits, layout.decimals, layout.unit_label)
        cached = self._geometry_cache.get(key)
        if cached is not None:
            return cached

        font = self._digit_font(size)
        metrics = QFontMetricsF(font)
        advance = metrics.horizontalAdvance("0")
        if advance <= 0:
            advance = size * 0.6

        point_cell = advance * POINT_CELL if layout.decimals > 0 else 0.0
        thin_cell = advance * THIN_CELL
        groups = max(0, (layout.decimals - 1) // 3) if layout.decimals > 0 else 0
        field = (1 + len(layout.slots)) * advance + point_cell + groups * thin_cell

        # The decimal point is placed by its *visible* centre, not by the
        # origin of its advance box.  Martian Mono is monospaced, so '.' has a
        # full digit advance with a left side bearing of about 0.35 of it;
        # drawing it at the cell origin pushed the visible dot clean out of
        # its cell and into the following digit.
        dot = metrics.tightBoundingRect(".")
        dot_offset = point_cell * DOT_CENTRE - (dot.x() + dot.width() / 2.0)

        unit_font = theme.readout(max(9, int(round(size * UNIT_SHARE))), QFont.Normal)
        unit_width = (
            QFontMetricsF(unit_font).horizontalAdvance(layout.unit_label)
            if layout.unit_label
            else 0.0
        )
        total = field + (advance * UNIT_GAP + unit_width if unit_width else 0.0)

        geometry = Geometry(
            size=size,
            advance=advance,
            point_cell=point_cell,
            thin_cell=thin_cell,
            dot_offset=dot_offset,
            unit_font=unit_font,
            unit_width=unit_width,
            field_width=field,
            total_width=total,
            cap_height=metrics.capHeight(),
        )
        self._geometry_cache[key] = geometry
        return geometry

    def _fit(self, layout: Layout, width: float, height: float) -> Geometry:
        """The largest digit size whose measured field still fits the glass."""
        probe = self._geometry(layout, 100)
        size = int(100.0 * width / max(1e-6, probe.total_width))
        size = min(size, int(height * 0.86))
        size = max(MIN_DIGIT_PX, min(MAX_DIGIT_PX, size))
        geometry = self._geometry(layout, size)
        # The scale is linear in principle and very nearly linear in practice;
        # step down rather than trust it, so nothing is ever clipped.
        while geometry.total_width > width and geometry.size > MIN_DIGIT_PX:
            geometry = self._geometry(layout, geometry.size - 1)
        return geometry

    def _paint_number(self, painter: QPainter, glass: QRectF) -> None:
        state = self._state
        inner = glass.adjusted(16, 8, -16, -8)
        if state is None:
            painter.setFont(theme.sans(13))
            painter.setPen(theme.C["dim"])
            painter.drawText(
                inner, int(Qt.AlignCenter), "Connecting to the instrument…"
            )
            return

        self._refit_if_needed()
        layout = self._layout
        if layout is None:
            return

        geometry = self._fit(layout, inner.width(), inner.height())
        font = self._digit_font(geometry.size)
        painter.setFont(font)
        baseline = inner.center().y() + geometry.cap_height / 2.0

        x = inner.left() + max(0.0, (inner.width() - geometry.total_width) / 2.0)

        if self._overload:
            self._paint_word(painter, font, x, baseline, geometry, "OVLD", theme.C["fail"])
        elif not self._have_value or self._value is None:
            self._paint_blank(painter, layout, x, baseline, geometry)
        else:
            self._paint_digits(painter, layout, x, baseline, geometry)

        if layout.unit_label:
            painter.setFont(geometry.unit_font)
            painter.setPen(theme.C["dim"])
            painter.drawText(
                QPointF(
                    x + geometry.field_width + geometry.advance * UNIT_GAP, baseline
                ),
                layout.unit_label,
            )

    def _paint_word(
        self,
        painter: QPainter,
        font: QFont,
        x: float,
        baseline: float,
        geometry: Geometry,
        word: str,
        colour: QColor,
    ) -> None:
        """Draw OVLD in the digit slots, so the geometry does not move.

        Every glyph in this face has the same advance as a digit and carries
        its own centring bearing, so stepping by one advance per character is
        exactly what natural text flow would do.
        """
        painter.setFont(font)
        painter.setPen(colour)
        cursor = x + geometry.advance  # skip the sign cell, as a number would
        for char in word:
            painter.drawText(QPointF(cursor, baseline), char)
            cursor += geometry.advance

    def _paint_point(self, painter: QPainter, cursor: float, baseline: float,
                     geometry: Geometry) -> None:
        """Draw the decimal point centred in its own cell by its visible ink."""
        painter.drawText(QPointF(cursor + geometry.dot_offset, baseline), ".")

    def _paint_blank(
        self, painter: QPainter, layout: Layout, x: float, baseline: float,
        geometry: Geometry,
    ) -> None:
        # A waiting display is quieter than a dimmed digit: the blank glyph is
        # the thin hyphen, not the en dash, and sits below the guard-digit
        # dimming so it never reads as a measurement.  The hyphen carries a
        # centring bearing of its own, so the cell origin is the right place
        # for it, exactly as for a digit.
        colour = QColor(theme.PHOSPHOR)
        colour.setAlphaF(BLANK_ALPHA)
        painter.setPen(colour)
        cursor = x + geometry.advance
        for index, _slot in enumerate(layout.slots):
            painter.drawText(QPointF(cursor, baseline), "-")
            cursor += geometry.advance
            separator = self._separator_width(layout, index, geometry)
            if separator and index == layout.int_digits - 1:
                self._paint_point(painter, cursor, baseline, geometry)
            cursor += separator

    def _separator_width(self, layout: Layout, index: int, geometry: Geometry) -> float:
        """Width of whatever follows the digit at *index*: point, thin space."""
        if index == layout.int_digits - 1 and layout.decimals > 0:
            return geometry.point_cell
        decimal_index = index - layout.int_digits + 1
        if (
            decimal_index > 0
            and decimal_index % 3 == 0
            and decimal_index < layout.decimals
        ):
            return geometry.thin_cell
        return 0.0

    def _paint_digits(
        self, painter: QPainter, layout: Layout, x: float, baseline: float,
        geometry: Geometry,
    ) -> None:
        value = float(self._value) / layout.mult
        negative = value < 0 or (value == 0 and math.copysign(1.0, value) < 0)
        text = f"{abs(value):.{layout.decimals}f}"
        integer_part, _, decimal_part = text.partition(".")

        if len(integer_part) > layout.int_digits:
            # The reading left the configured range.  Widen once, honestly,
            # rather than dropping the leading digits.
            widened = compute_layout(self._state or {}, self._value)
            if len(integer_part) > widened.int_digits:
                widened = Layout(
                    mult=widened.mult,
                    prefix=widened.prefix,
                    no_prefix=widened.no_prefix,
                    int_digits=min(9, len(integer_part)),
                    decimals=widened.decimals,
                    res_scaled=widened.res_scaled,
                    unit=widened.unit,
                    func=widened.func,
                    slots=_rebuild_slots(
                        min(9, len(integer_part)), widened.decimals, widened.res_scaled
                    ),
                )
            self._layout = widened
            layout = widened
            value = float(self._value) / layout.mult
            text = f"{abs(value):.{layout.decimals}f}"
            integer_part, _, decimal_part = text.partition(".")

        chars = [" "] * (layout.int_digits - len(integer_part))
        chars.extend(integer_part)
        chars.extend(decimal_part)

        solid = QColor(theme.PHOSPHOR)
        dimmed = QColor(theme.PHOSPHOR)
        dimmed.setAlphaF(DIM_ALPHA)

        if negative:
            # U+2212 is a real glyph in this face with a full digit advance and
            # its own centring bearing, so the sign cell origin is correct.
            painter.setPen(solid)
            painter.drawText(QPointF(x, baseline), MINUS)

        cursor = x + geometry.advance
        for index, slot in enumerate(layout.slots):
            char = chars[index] if index < len(chars) else " "
            if char != " ":
                painter.setPen(dimmed if slot.dim else solid)
                painter.drawText(QPointF(cursor, baseline), char)
            cursor += geometry.advance
            separator = self._separator_width(layout, index, geometry)
            if separator and index == layout.int_digits - 1:
                painter.setPen(solid)
                self._paint_point(painter, cursor, baseline, geometry)
            cursor += separator

    # ---------------------------------------------------------------- band

    def _no_band_caption(self) -> str:
        """Say why there is no resolution band, in the terms that apply.

        Two different reasons, and conflating them would be misleading: on
        CAP/FREQ/PER/TEMP/CONT/DIOD the instrument has no ``<p>:RES?`` to
        report, whereas under dB scaling it has one and it is the wrong
        quantity.  Neither case invents an accuracy figure.
        """
        state = self._state or {}
        func = str(state.get("func") or "")
        scaled = scale_unit(state)
        if scaled:
            resolution = state.get("resolution")
            base = base_unit(state)
            step = (
                fmt_si(resolution, base, 3, False, max_exponent(base))
                if is_num(resolution) and float(resolution) > 0
                else ""
            )
            reported = f" — it reports {step}" if step else ""
            return (
                f"resolution is not reported for {scaled} scaling: "
                f"{func}:RES? is in {base or 'the measurement unit'}{reported}, "
                f"which does not describe a logarithmic reading. Every digit "
                f"is solid and no accuracy figure is shown."
            )
        return (
            f"{func}:RES? is not queryable on this function — every digit is "
            f"solid, and no accuracy figure is shown."
        )

    def _paint_band(self, painter: QPainter, rect: QRectF) -> None:
        state = self._state
        if state is None:
            return
        resolution = effective_resolution(state)
        func = str(state.get("func") or "")

        if resolution is None:
            painter.setFont(theme.caption())
            painter.setPen(theme.C["dim"])
            text = self._no_band_caption()
            metrics = QFontMetricsF(painter.font())
            flags = int(Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap)
            if metrics.boundingRect(rect, flags, text).height() > rect.height():
                # Not enough room even wrapped — a window dragged very narrow.
                # Elide the tail so it ends in an ellipsis instead of being
                # cut mid-word, and leave the whole sentence one hover away.
                lines = max(1, int(rect.height() // max(1.0, metrics.height())))
                text = _elide_to_lines(metrics, text, rect.width(), lines)
            painter.drawText(rect, flags, text)
            return

        layout = self._layout
        track = QRectF(rect.left(), rect.top() + 4, rect.width(), 9)

        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.C["panel2"])
        painter.drawRect(track)

        painter.setPen(QPen(theme.C["rule"], 1))
        for index in range(11):
            x = track.left() + track.width() * index / 10.0
            height = track.height() if index in (0, 5, 10) else track.height() * 0.55
            painter.drawLine(
                QPointF(x, track.bottom()), QPointF(x, track.bottom() - height)
            )

        if self._have_value and self._value is not None and not self._overload:
            quantum = float(self._value) / resolution
            residual = quantum - round(quantum)  # −0.5 .. +0.5 of one step
            x = track.left() + track.width() * (0.5 + residual * 0.1)
            needle = QPainterPath()
            needle.moveTo(x, track.top() - 1)
            needle.lineTo(x - 4, track.top() - 7)
            needle.lineTo(x + 4, track.top() - 7)
            needle.closeSubpath()
            painter.setPen(Qt.NoPen)
            painter.setBrush(theme.C["signal"])
            painter.drawPath(needle)

        painter.setFont(theme.caption())
        text_rect = QRectF(rect.left(), track.bottom() + 3, rect.width(), 14)
        painter.setPen(theme.C["dim"])
        step = fmt_state(resolution, state, 3)
        painter.drawText(
            text_rect,
            int(Qt.AlignLeft | Qt.AlignVCenter),
            f"± {step} — one resolution step, on a ±5 step track",
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignRight | Qt.AlignVCenter),
            node_for(func, "resolution"),
        )

        if (
            self._have_value
            and self._value is not None
            and not self._overload
            and layout is not None
        ):
            painter.setFont(theme.mono(10))
            low = (float(self._value) - 5 * resolution) / layout.mult
            high = (float(self._value) + 5 * resolution) / layout.mult
            ends = QRectF(rect.left(), track.top() - 20, rect.width(), 14)
            painter.setPen(theme.C["dim"])
            painter.drawText(
                ends,
                int(Qt.AlignLeft | Qt.AlignVCenter),
                f"{low:.{layout.decimals}f} {layout.unit_label}",
            )
            painter.drawText(
                ends,
                int(Qt.AlignRight | Qt.AlignVCenter),
                f"{high:.{layout.decimals}f} {layout.unit_label}",
            )


def _elide_to_lines(
    metrics: QFontMetricsF, text: str, width: float, lines: int
) -> str:
    """*text* wrapped to at most *lines* lines, the last one elided.

    Qt's own eliding is single-line, so the text is wrapped by hand and only
    the final line is shortened — the reader still gets as much of the
    sentence as the space allows, ending in an ellipsis that says there is
    more, with the full text in the tooltip.
    """
    words = text.split()
    rows: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and metrics.horizontalAdvance(candidate) > width:
            rows.append(current)
            current = word
            if len(rows) == lines:
                break
        else:
            current = candidate
    if len(rows) < lines and current:
        rows.append(current)
    if len(rows) < lines:
        return text
    consumed = len(" ".join(rows))
    remainder = text[consumed:].strip()
    if remainder:
        rows[-1] = metrics.elidedText(
            f"{rows[-1]} {remainder}", Qt.ElideRight, width
        )
    return "\n".join(rows)


def _rebuild_slots(
    int_digits: int, decimals: int, res_scaled: Optional[float]
) -> Tuple[Slot, ...]:
    slots: List[Slot] = []
    for index in range(int_digits):
        slots.append(Slot(10.0 ** (int_digits - 1 - index), False))
    for decimal in range(1, decimals + 1):
        place = 10.0 ** (-decimal)
        slots.append(
            Slot(place, res_scaled is not None and place < res_scaled * (1 - 1e-9))
        )
    return tuple(slots)
