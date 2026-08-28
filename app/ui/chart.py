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

"""Live strip chart, drawn with ``QPainter``.  No QtCharts dependency.

Ported from ``reference/web/chart.js`` (``StripChart``).  Two corrections were
made while porting:

* the reference dropped non-finite samples entirely (``continue``), which made
  the trace draw a straight line *through* an overload.  Overloads are stored
  as NaN here and break the polyline, so an overload reads as a gap.
* the reference's per-column min/max scan walked the ring in Python for every
  column.  The same decimation is done with two ``np.searchsorted`` calls and
  ``np.fmin/np.fmax.reduceat``, which is NaN-aware and keeps a full-width
  repaint inside the 30 Hz budget at 740 readings/s.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .units import (
    fmt_si,
    is_num,
    max_exponent,
    si_step,
    unit_no_prefix,
    unit_text,
)

CAPACITY = 262144  # ARCHITECTURE.md section 7: at least 262144 points
PAD_LEFT = 74
PAD_RIGHT = 16
PAD_TOP = 12
PAD_BOTTOM = 26

# Autoscale behaviour.  The axis is decided on the paint tick, at this rate,
# and never inside paintEvent: painting renders the scale, it does not choose
# one, so a resize or an expose can never move the axis.
AUTOSCALE_PERIOD = 0.25  # s — 4 Hz, not 30 Hz
SHRINK_RATIO = 0.60  # data must fit this fraction of the range before shrinking
SHRINK_DELAY = 0.75  # s — and stay there this long
LIMIT_REACH = 25.0  # a limit is folded in only if the range stays within
# this many data spans of it; further out it would collapse the trace to a line


def nice_step(raw: float) -> float:
    """A 1/2/5-times-a-decade tick step at or below *raw*."""
    if not (raw > 0) or not math.isfinite(raw):
        return 1.0
    exponent = math.floor(math.log10(raw))
    fraction = raw / (10.0 ** exponent)
    lead = 5.0 if fraction >= 5 else 2.0 if fraction >= 2 else 1.0
    return lead * (10.0 ** exponent)


class StripChart(QWidget):
    """Ring-buffered strip chart with min/max decimation per pixel column."""

    cursorMoved = Signal(object)  # {"t":..., "v":...} or None

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._ts = np.zeros(CAPACITY, dtype=np.float64)
        self._vs = np.zeros(CAPACITY, dtype=np.float64)
        self._n = 0
        self._total = 0

        self._t0: Optional[float] = None
        self._prev_rel = 0.0

        self.unit = ""
        self.no_prefix = False
        self.max_exp: Optional[int] = None

        self.window = 60.0
        self.follow = True
        self.x_min = 0.0
        self.x_max = 60.0
        self.y_auto = True
        self.y_min = -1.0
        self.y_max = 1.0

        self._limits = {"on": False, "low": None, "high": None}
        self._scale_at = 0.0
        self._shrink_since: Optional[float] = None
        self._cursor: Optional[QPointF] = None
        self._drag: Optional[Tuple[float, float, float, float, float]] = None
        self._dirty = False

        self.empty_text = "Press Run — streamed readings fill this chart."

    # ------------------------------------------------------------------ data

    def count(self) -> int:
        return self._n

    def clear(self) -> None:
        self._n = 0
        self._total = 0
        self._t0 = None
        self._prev_rel = 0.0
        self.x_min = 0.0
        self.x_max = self.window
        self.y_min, self.y_max = -1.0, 1.0
        self._shrink_since = None
        self._scale_at = 0.0
        self._dirty = True

    def _make_room(self, wanted: int) -> None:
        """Keep the samples contiguous so searchsorted and reduceat apply.

        When the buffer fills, the oldest half is discarded in one move.  A
        wrapping ring would need the same work split across every read.
        """
        if self._n + wanted <= CAPACITY:
            return
        keep = CAPACITY // 2
        if wanted >= CAPACITY:
            self._n = 0
            return
        if keep + wanted > CAPACITY:
            keep = CAPACITY - wanted
        if keep > 0:
            self._ts[:keep] = self._ts[self._n - keep : self._n].copy()
            self._vs[:keep] = self._vs[self._n - keep : self._n].copy()
        self._n = max(0, keep)

    def push_batch(
        self, t_epoch: float, values: List[Optional[float]], rate_hz: float
    ) -> None:
        """Add one ``{type:"data"}`` message.

        The wire format carries one timestamp for the whole batch, so sample
        times inside it are interpolated from the previous batch's timestamp.
        *rate_hz* is used only for the first batch, before a delta exists.
        An overloaded sample arrives as ``None`` and is stored as NaN, which
        both breaks the trace and drops out of autoscale.
        """
        if not values:
            return
        count = len(values)
        if self._t0 is None:
            lead = count / rate_hz if rate_hz > 0 else count * 0.01
            self._t0 = t_epoch - lead
            self._prev_rel = 0.0
        t_rel = t_epoch - self._t0
        previous = self._prev_rel
        if t_rel <= previous:
            t_rel = previous + (count / rate_hz if rate_hz > 0 else 0.001 * count)
        step = (t_rel - previous) / count

        self._make_room(count)
        start = self._n
        end = start + count
        self._ts[start:end] = previous + step * np.arange(1, count + 1, dtype=np.float64)
        self._vs[start:end] = np.array(
            [math.nan if v is None else float(v) for v in values], dtype=np.float64
        )
        self._n = end
        self._total += count
        self._prev_rel = t_rel

        if self.follow:
            self._follow_window()
        self._dirty = True

    def _follow_window(self) -> None:
        last = self._ts[self._n - 1] if self._n else 0.0
        self.x_max = max(self.window, last + self.window * 0.02)
        self.x_min = self.x_max - self.window

    # --------------------------------------------------------------- config

    def set_unit(self, unit: str, no_prefix: bool, max_exp: Optional[int]) -> None:
        self.unit = unit
        self.no_prefix = no_prefix
        self.max_exp = max_exp
        self._dirty = True

    def set_limits(self, on: bool, low: Any, high: Any) -> None:
        self._limits = {"on": bool(on), "low": low, "high": high}
        self._dirty = True

    def set_window(self, seconds: float) -> None:
        self.window = max(0.5, float(seconds))
        if self.follow:
            self._follow_window()
        else:
            centre = (self.x_min + self.x_max) / 2.0
            self.x_min = centre - self.window / 2.0
            self.x_max = centre + self.window / 2.0
        self._dirty = True

    def set_follow(self, on: bool) -> None:
        self.follow = bool(on)
        if self.follow:
            self._follow_window()
        self._dirty = True

    def set_y_auto(self, on: bool) -> None:
        self.y_auto = bool(on)
        self._dirty = True

    def fit_all(self) -> None:
        if self._n == 0:
            self.x_min = 0.0
            self.x_max = self.window
        else:
            first = float(self._ts[0])
            last = float(self._ts[self._n - 1])
            if last - first < 1e-6:
                last = first + 1.0
            pad = (last - first) * 0.02
            self.x_min = first - pad
            self.x_max = last + pad
            self.follow = False
        self.fit_y()
        self._dirty = True

    # ------------------------------------------------------------ repainting

    def tick(self) -> None:
        """Called by the application's 30 Hz timer; coalesces repaints."""
        if self.evaluate_autoscale(time.monotonic()):
            self._dirty = True
        if self._dirty:
            self._dirty = False
            self.update()

    def mark_dirty(self) -> None:
        self._dirty = True

    # ------------------------------------------------------------ geometry

    def _plot_rect(self) -> QRectF:
        return QRectF(
            PAD_LEFT,
            PAD_TOP,
            max(1.0, self.width() - PAD_LEFT - PAD_RIGHT),
            max(1.0, self.height() - PAD_TOP - PAD_BOTTOM),
        )

    def _t_to_x(self, t: float, plot: QRectF) -> float:
        span = max(1e-12, self.x_max - self.x_min)
        return plot.left() + (t - self.x_min) / span * plot.width()

    def _x_to_t(self, x: float, plot: QRectF) -> float:
        return self.x_min + (x - plot.left()) / plot.width() * (self.x_max - self.x_min)

    def _v_to_y(self, v: float, plot: QRectF) -> float:
        span = max(1e-12, self.y_max - self.y_min)
        return plot.top() + (self.y_max - v) / span * plot.height()

    def _y_to_v(self, y: float, plot: QRectF) -> float:
        return self.y_max - (y - plot.top()) / plot.height() * (self.y_max - self.y_min)

    def _visible_slice(self) -> Tuple[int, int]:
        if self._n == 0:
            return 0, 0
        times = self._ts[: self._n]
        lo = int(np.searchsorted(times, self.x_min, side="left"))
        hi = int(np.searchsorted(times, self.x_max, side="right"))
        return max(0, lo - 1), min(self._n, hi + 1)

    def _visible_extent(self) -> Optional[Tuple[float, float]]:
        """Min and max of the finite samples in view, plus qualified limits."""
        lo, hi = self._visible_slice()
        if hi <= lo:
            return None
        window = self._vs[lo:hi]
        if not bool(np.isfinite(window).any()):
            return None
        low = float(np.nanmin(window))
        high = float(np.nanmax(window))

        # A limit line belongs in the range only when it is really set.  The
        # instrument's limits default to 0 and 0, so folding them in blindly
        # drags a small signal riding on a DC offset flat against the edge.
        # A genuinely set limit is worth showing even when it is well outside
        # the data — that is what limits are for — but one far enough away to
        # collapse the trace to a line is not, so the fold is bounded.
        low_bound = self._limits["low"]
        high_bound = self._limits["high"]
        if (
            self._limits["on"]
            and is_num(low_bound)
            and is_num(high_bound)
            and (float(low_bound) != 0.0 or float(high_bound) != 0.0)
        ):
            data_low, data_high = low, high
            data_span = max(data_high - data_low, abs(data_high) * 1e-9, 1e-12)
            allowed = data_span * LIMIT_REACH * (1 + 1e-9)
            # Each bound is judged against the data alone, so the verdict does
            # not depend on which one is considered first.
            for bound in (float(low_bound), float(high_bound)):
                if max(data_high, bound) - min(data_low, bound) <= allowed:
                    low = min(low, bound)
                    high = max(high, bound)
        return low, high

    @staticmethod
    def _nice_bounds(low: float, high: float) -> Tuple[float, float, float]:
        """Pad, then snap outward to whole steps so gridlines sit still."""
        if high - low < 1e-15:
            pad = max(abs(high) * 1e-6, 1e-9)
            low -= pad
            high += pad
        margin = (high - low) * 0.08
        low -= margin
        high += margin
        step = nice_step((high - low) / 5.0)
        snapped_low = math.floor(low / step + 1e-9) * step
        snapped_high = math.ceil(high / step - 1e-9) * step
        if snapped_high - snapped_low < step:
            snapped_high = snapped_low + step
        return snapped_low, snapped_high, step

    def evaluate_autoscale(self, now: float, immediate: bool = False) -> bool:
        """Decide the Y range.  Called from the paint tick, never from paint.

        Growth is applied at once — the signal is never clipped.  Shrinking
        waits until the data has stayed well inside the current range for a
        sustained period, so a noisy trace does not make the axis breathe.
        """
        if not self.y_auto and not immediate:
            return False
        if not immediate and now - self._scale_at < AUTOSCALE_PERIOD:
            return False
        self._scale_at = now

        extent = self._visible_extent()
        if extent is None:
            return False
        low, high = self._nice_bounds(*extent)[:2]
        current = self.y_max - self.y_min

        if immediate or current <= 0:
            self.y_min, self.y_max = low, high
            self._shrink_since = None
            return True

        if low < self.y_min - 1e-12 or high > self.y_max + 1e-12:
            # Grow: keep whatever was already visible, add what is new.
            self.y_min = min(self.y_min, low)
            self.y_max = max(self.y_max, high)
            self._shrink_since = None
            return True

        if (high - low) <= current * SHRINK_RATIO:
            if self._shrink_since is None:
                self._shrink_since = now
            elif now - self._shrink_since >= SHRINK_DELAY:
                self.y_min, self.y_max = low, high
                self._shrink_since = None
                return True
        else:
            self._shrink_since = None
        return False

    def fit_y(self) -> None:
        """One-shot fit to the data in view; re-enables the automatic scale."""
        self.y_auto = True
        self._shrink_since = None
        self.evaluate_autoscale(time.monotonic(), immediate=True)
        self._dirty = True

    def _axis_scale(self) -> Tuple[float, str]:
        """One SI prefix for the whole value axis, so labels keep their width."""
        if self.no_prefix:
            return 1.0, self.unit
        extreme = max(abs(self.y_min), abs(self.y_max))
        if extreme <= 0:
            return 1.0, self.unit
        mult, prefix = si_step(extreme, self.max_exp)
        return mult, prefix + self.unit

    # ---------------------------------------------------------------- paint

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), theme.C["ink"])

        plot = self._plot_rect()
        self._paint_grid(painter, plot)
        if self._n == 0:
            painter.setFont(theme.sans(12))
            painter.setPen(theme.C["dim"])
            painter.drawText(plot, int(Qt.AlignCenter), self.empty_text)
        else:
            self._paint_trace(painter, plot)
        self._paint_limits(painter, plot)
        painter.setPen(QPen(theme.C["rule"], 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(plot.adjusted(0.5, 0.5, -0.5, -0.5))
        self._paint_crosshair(painter, plot)
        painter.end()

    def _paint_grid(self, painter: QPainter, plot: QRectF) -> None:
        painter.setFont(theme.mono(10))
        metrics = QFontMetricsF(painter.font())
        grid = QPen(QColor(theme.RULE), 1)
        grid.setStyle(Qt.SolidLine)

        y_span = self.y_max - self.y_min
        step = nice_step(y_span / 5.0) if y_span > 0 else 1.0
        # One prefix for the whole axis and a decimal count fixed by the step,
        # so every label is the same width: a label that changes width as the
        # value crosses a digit boundary makes the whole plot shimmer.
        mult, axis_unit = self._axis_scale()
        scaled_step = step / mult
        decimals = 0
        if scaled_step > 0:
            decimals = max(0, min(9, -int(math.floor(math.log10(scaled_step) + 1e-9))))
        # Ticks are integer multiples of the step, never an accumulated sum:
        # adding 0.2 five times from -1 lands on -5.55e-17, which the SI
        # formatter would print as "-0.0555 f" instead of "0".
        index = math.ceil(self.y_min / step - 1e-9)
        while True:
            value = index * step
            index += 1
            if value > self.y_max + step * 1e-6:
                break
            y = self._v_to_y(value, plot)
            painter.setPen(grid)
            painter.drawLine(
                QPointF(plot.left(), round(y) + 0.5),
                QPointF(plot.right(), round(y) + 0.5),
            )
            painter.setPen(theme.C["dim"])
            scaled = value / mult
            if abs(scaled) < 10.0 ** (-decimals) / 2.0:
                scaled = 0.0
            text = f"{scaled:.{decimals}f}"
            painter.drawText(
                QRectF(0, y - 8, PAD_LEFT - 8, 16),
                int(Qt.AlignRight | Qt.AlignVCenter),
                f"{text} {axis_unit}" if axis_unit else text,
            )

        x_span = self.x_max - self.x_min
        x_step = nice_step(x_span / 6.0) if x_span > 0 else 1.0
        index = math.ceil(self.x_min / x_step - 1e-9)
        while True:
            value = index * x_step
            index += 1
            if value > self.x_max + x_step * 1e-6:
                break
            x = self._t_to_x(value, plot)
            painter.setPen(grid)
            painter.drawLine(
                QPointF(round(x) + 0.5, plot.top()),
                QPointF(round(x) + 0.5, plot.bottom()),
            )
            painter.setPen(theme.C["dim"])
            text = f"{value:g} s"
            painter.drawText(
                QRectF(x - 40, plot.bottom() + 4, 80, 16),
                int(Qt.AlignCenter),
                text,
            )
        del metrics

    def _paint_trace(self, painter: QPainter, plot: QRectF) -> None:
        lo, hi = self._visible_slice()
        if hi <= lo:
            return
        times = self._ts[lo:hi]
        values = self._vs[lo:hi]
        columns = max(1, int(plot.width()))

        edges = self.x_min + (self.x_max - self.x_min) * (
            np.arange(columns + 1, dtype=np.float64) / columns
        )
        starts = np.searchsorted(times, edges)
        counts = starts[1:] - starts[:-1]
        heads = np.clip(starts[:-1], 0, len(times) - 1)
        tails = np.clip(starts[1:] - 1, 0, len(times) - 1)
        # fmin/fmax ignore NaN, so an all-overload column reduces to NaN and is
        # drawn as a gap rather than as a spike or an interpolated line.
        mins = np.fmin.reduceat(values, heads)
        maxs = np.fmax.reduceat(values, heads)
        firsts = values[heads]
        lasts = values[tails]

        painter.save()
        painter.setClipRect(plot)
        pen = QPen(theme.C["signal"], 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)

        segments: List[Tuple[QPointF, QPointF]] = []
        previous: Optional[QPointF] = None
        for column in range(columns):
            if counts[column] <= 0:
                continue
            low = mins[column]
            high = maxs[column]
            if not math.isfinite(low) or not math.isfinite(high):
                previous = None  # every sample in this column overloaded
                continue
            x = plot.left() + column + 0.5
            y_high = self._v_to_y(float(high), plot)
            y_low = self._v_to_y(float(low), plot)
            head = firsts[column]
            tail = lasts[column]
            head_y = self._v_to_y(float(head), plot) if math.isfinite(head) else y_high
            tail_y = self._v_to_y(float(tail), plot) if math.isfinite(tail) else y_low
            if previous is not None:
                segments.append((previous, QPointF(x, head_y)))
            segments.append((QPointF(x, y_high), QPointF(x, max(y_low, y_high + 0.6))))
            previous = QPointF(x, tail_y)

        for a, b in segments:
            painter.drawLine(a, b)

        last_value = self._vs[self._n - 1]
        if math.isfinite(last_value):
            x = self._t_to_x(float(self._ts[self._n - 1]), plot)
            y = self._v_to_y(float(last_value), plot)
            if plot.contains(QPointF(x, y)):
                painter.setPen(Qt.NoPen)
                painter.setBrush(theme.C["phosphor"])
                painter.drawRect(QRectF(x - 1.5, y - 1.5, 3, 3))
        painter.restore()

    def _paint_limits(self, painter: QPainter, plot: QRectF) -> None:
        if not self._limits["on"]:
            return
        painter.save()
        painter.setClipRect(plot)
        painter.setFont(theme.mono(9))
        for bound, label in (
            (self._limits["low"], "LOW"),
            (self._limits["high"], "HIGH"),
        ):
            if not is_num(bound):
                continue
            y = self._v_to_y(float(bound), plot)
            if y < plot.top() or y > plot.bottom():
                continue
            pen = QPen(theme.C["warn"], 1)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(
                QPointF(plot.left(), round(y) + 0.5),
                QPointF(plot.right(), round(y) + 0.5),
            )
            painter.setPen(theme.C["warn"])
            painter.drawText(
                QPointF(plot.left() + 6, y - 3),
                f"{label} "
                + fmt_si(bound, self.unit, 5, self.no_prefix, self.max_exp),
            )
        painter.restore()

    def _nearest(self, t: float) -> Optional[int]:
        if self._n == 0:
            return None
        times = self._ts[: self._n]
        index = int(np.searchsorted(times, t))
        best = None
        best_distance = math.inf
        for candidate in (index - 1, index, index + 1):
            if candidate < 0 or candidate >= self._n:
                continue
            distance = abs(float(times[candidate]) - t)
            if distance < best_distance:
                best_distance = distance
                best = candidate
        return best

    def _paint_crosshair(self, painter: QPainter, plot: QRectF) -> None:
        cursor = self._cursor
        if cursor is None or not plot.contains(cursor):
            return
        index = self._nearest(self._x_to_t(cursor.x(), plot))
        if index is None:
            return
        sample_t = float(self._ts[index])
        sample_v = float(self._vs[index])
        x = self._t_to_x(sample_t, plot)

        painter.save()
        painter.setClipRect(plot)
        pen = QPen(theme.C["dim"], 1)
        pen.setStyle(Qt.DotLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        if math.isfinite(sample_v):
            y = self._v_to_y(sample_v, plot)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(Qt.NoPen)
            painter.setBrush(theme.C["phosphor"])
            painter.drawEllipse(QPointF(x, y), 2.5, 2.5)
        painter.restore()

        text = (
            f"{sample_t:.3f} s   "
            + (
                fmt_si(sample_v, self.unit, 6, self.no_prefix, self.max_exp)
                if math.isfinite(sample_v)
                else "OVLD"
            )
        )
        painter.setFont(theme.mono(10))
        metrics = QFontMetricsF(painter.font())
        width = metrics.horizontalAdvance(text) + 12
        box = QRectF(
            min(cursor.x() + 12, plot.right() - width), plot.top() + 6, width, 18
        )
        painter.setPen(QPen(theme.C["rule"], 1))
        painter.setBrush(theme.C["panel2"])
        painter.drawRect(box)
        painter.setPen(
            theme.C["phosphor"] if math.isfinite(sample_v) else theme.C["fail"]
        )
        painter.drawText(box, int(Qt.AlignCenter), text)

    # ------------------------------------------------------------ interaction

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        position = event.position()
        self._cursor = QPointF(position)
        if self._drag is not None:
            plot = self._plot_rect()
            start_x, start_y, x_min, x_max, y_state = self._drag
            span = self.x_max - self.x_min
            dt = (position.x() - start_x) / plot.width() * span
            self.x_min = x_min - dt
            self.x_max = x_max - dt
            self.follow = False
            if not self.y_auto:
                y_min, y_max = y_state
                y_span = y_max - y_min
                dv = (position.y() - start_y) / plot.height() * y_span
                self.y_min = y_min + dv
                self.y_max = y_max + dv
        index = self._nearest(self._x_to_t(position.x(), self._plot_rect()))
        if index is None:
            self.cursorMoved.emit(None)
        else:
            value = float(self._vs[index])
            self.cursorMoved.emit(
                {
                    "t": float(self._ts[index]),
                    "v": value if math.isfinite(value) else None,
                }
            )
        self._dirty = True

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._cursor = None
        self.cursorMoved.emit(None)
        self._dirty = True

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            position = event.position()
            self._drag = (
                position.x(),
                position.y(),
                self.x_min,
                self.x_max,
                (self.y_min, self.y_max),
            )
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag = None
            self.unsetCursor()

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        factor = 0.85 ** steps
        plot = self._plot_rect()
        if event.modifiers() & Qt.ShiftModifier:
            self.y_auto = False
            anchor = self._y_to_v(event.position().y(), plot)
            self.y_min = anchor + (self.y_min - anchor) * factor
            self.y_max = anchor + (self.y_max - anchor) * factor
        else:
            anchor = self._x_to_t(event.position().x(), plot)
            self.x_min = anchor + (self.x_min - anchor) * factor
            self.x_max = anchor + (self.x_max - anchor) * factor
            self.window = self.x_max - self.x_min
            self.follow = False
        self._dirty = True
        event.accept()


class ChartPanel(QWidget):
    """The chart plus its own controls, as the Chart tab shows it."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.chart = StripChart(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        controls.addWidget(_small_label("WINDOW"))
        self.window_box = QComboBox()
        for seconds, text in (
            (10, "10 s"),
            (30, "30 s"),
            (60, "60 s"),
            (300, "5 min"),
            (900, "15 min"),
            (3600, "60 min"),
        ):
            self.window_box.addItem(text, seconds)
        self.window_box.setCurrentIndex(2)
        self.window_box.currentIndexChanged.connect(self._window_changed)
        controls.addWidget(self.window_box)

        self.follow_box = QCheckBox("Follow")
        self.follow_box.setChecked(True)
        self.follow_box.toggled.connect(self.chart.set_follow)
        controls.addWidget(self.follow_box)

        self.yauto_box = QCheckBox("Auto Y")
        self.yauto_box.setChecked(True)
        self.yauto_box.toggled.connect(self.chart.set_y_auto)
        controls.addWidget(self.yauto_box)

        self.fity_button = QPushButton("Fit")
        self.fity_button.setToolTip(
            "Fit the value axis to what is on screen now, and re-enable Auto Y"
        )
        self.fity_button.clicked.connect(self._fit_y)
        controls.addWidget(self.fity_button)

        self.fit_button = QPushButton("Fit all")
        self.fit_button.setToolTip("Show the whole run, then fit the value axis")
        self.fit_button.clicked.connect(self._fit)
        controls.addWidget(self.fit_button)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._clear)
        controls.addWidget(self.clear_button)

        controls.addStretch(1)
        self.readout = QLabel("—")
        self.readout.setObjectName("Value")
        self.readout.setMinimumWidth(240)
        self.readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        controls.addWidget(self.readout)
        layout.addLayout(controls)
        layout.addWidget(self.chart, 1)

        hint = QLabel(
            "Drag to pan · wheel to zoom time · shift+wheel to zoom value · "
            "overloads are drawn as gaps"
        )
        hint.setObjectName("Caption")
        layout.addWidget(hint)

        self.chart.cursorMoved.connect(self._cursor)
        self._state: Optional[Dict[str, Any]] = None

    def _window_changed(self, index: int) -> None:
        seconds = self.window_box.itemData(index)
        if seconds:
            self.chart.set_window(float(seconds))

    def _fit(self) -> None:
        self.chart.fit_all()
        self.follow_box.setChecked(self.chart.follow)
        self.yauto_box.setChecked(True)

    def _fit_y(self) -> None:
        self.chart.fit_y()
        self.yauto_box.setChecked(True)
        self.chart.update()

    def _clear(self) -> None:
        self.chart.clear()
        self.chart.update()

    def _cursor(self, sample: Optional[Dict[str, Any]]) -> None:
        if sample is None:
            self.readout.setText("—")
            return
        value = sample.get("v")
        text = (
            fmt_si(value, self.chart.unit, 6, self.chart.no_prefix, self.chart.max_exp)
            if value is not None
            else "OVLD"
        )
        self.readout.setText(f"{sample['t']:.3f} s     {text}")

    def apply_state(self, state: Dict[str, Any]) -> None:
        self._state = state
        unit = unit_text(state)
        self.chart.set_unit(unit, unit_no_prefix(state), max_exponent(unit))
        math_state = state.get("math") or {}
        self.chart.set_limits(
            bool(math_state.get("limit_on")),
            math_state.get("limit_low"),
            math_state.get("limit_high"),
        )


def _small_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionLabel")
    label.setFont(theme.label())
    return label
