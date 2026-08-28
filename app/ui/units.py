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

"""Units, SI formatting and SCPI-node captions.

Ported from ``reference/web/chart.js`` (``fmtSI``/``trimNum``) and
``reference/web/app.js`` (``unitText``, ``maxExpFor``, ``nodeFor``).  The logic
was derived from this instrument's real behaviour; the corrections made while
porting are noted inline.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

MINUS = "−"  # U+2212 MINUS SIGN, the width of a digit in these faces
THIN = " "  # thin space, the instrument's own decimal group separator
OHM = "Ω"
MICRO = "µ"

# (multiplier, prefix), descending.  femto is the smallest the 34461A needs.
SI_STEPS: List[Tuple[float, str]] = [
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1.0, ""),
    (1e-3, "m"),
    (1e-6, MICRO),
    (1e-9, "n"),
    (1e-12, "p"),
    (1e-15, "f"),
]

SI_BY_EXP: Dict[int, str] = {
    9: "G",
    6: "M",
    3: "k",
    0: "",
    -3: "m",
    -6: MICRO,
    -9: "n",
    -12: "p",
    -15: "f",
}

# Functions whose :RANG node is expressed in the same unit as the reading.
# FREQ and PER borrow FREQ:VOLT:RANG — a *voltage* range — so sizing the
# readout from it would be wrong (ARCHITECTURE.md section 3).
RANGE_IS_READING_UNIT = frozenset(
    ("VOLT:DC", "VOLT:AC", "CURR:DC", "CURR:AC", "RES", "FRES", "CAP")
)


def is_num(value: Any) -> bool:
    """True for a real, finite number (``None`` and NaN are not)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# CALC:SCAL:FUNC values that change what the reading *is*.  DB and DBM turn it
# into a logarithmic ratio, so it is no longer in the function's own unit —
# a reading of -0.99 under DB scaling is -0.99 dB, not -0.99 V.
#
# NULL is deliberately absent: CALC:SCAL:FUNC NULL subtracts a reference in the
# function's own unit and the result is still volts, amps or ohms.  It agrees
# with the per-function <p>:NULL:* path, which is the same operation reached
# through a different node — both leave the unit alone, and both leave
# <p>:RES? meaningful, because a difference of two volts is still a voltage.
LOG_SCALE_UNITS: Dict[str, str] = {"DB": "dB", "DBM": "dBm"}


def scale_unit(state: Optional[Dict[str, Any]]) -> str:
    """``"dB"``/``"dBm"`` when the instrument is scaling logarithmically.

    Empty for everything else, including ``CALC:SCAL:FUNC NULL`` and scaling
    that is configured but switched off.
    """
    if not state:
        return ""
    math_state = state.get("math") or {}
    if not math_state.get("scale_on"):
        return ""
    token = str(math_state.get("scale_func") or "").strip().upper()
    return LOG_SCALE_UNITS.get(token, "")


def base_unit(state: Optional[Dict[str, Any]]) -> str:
    """The measurement function's own unit, ignoring any scaling.

    ohm -> Omega, deg -> degC/degF/K.  This is what ``<p>:RANG`` and
    ``<p>:RES?`` are expressed in, so it is what those must be formatted with
    even when the reading itself has been scaled into dB.
    """
    if not state:
        return ""
    unit = state.get("unit") or ""
    if unit.lower() == "ohm":
        return OHM
    if unit == "deg":
        temp = state.get("temp") or {}
        token = str(temp.get("unit") or "C").upper()
        if token.startswith("K"):
            return "K"
        return "°" + ("F" if token.startswith("F") else "C")
    return unit


def unit_text(state: Optional[Dict[str, Any]]) -> str:
    """The unit the *reading* is in — dB/dBm under logarithmic scaling.

    Every panel that shows a measured value derives its unit from here (the
    readout, the chart axis, the statistics, the histogram, the log table), so
    they cannot disagree about it.
    """
    return scale_unit(state) or base_unit(state)


def unit_no_prefix(state: Optional[Dict[str, Any]]) -> bool:
    """Units that never take an SI prefix.

    Temperature: 21.5 degC, not 21.5 m degC.  And dB/dBm, which are already
    logarithmic — "mdB" is not a unit.
    """
    if not state:
        return False
    return state.get("unit") == "deg" or bool(scale_unit(state))


def effective_resolution(state: Optional[Dict[str, Any]]) -> Optional[float]:
    """``<p>:RES?`` when it applies to the displayed reading, else ``None``.

    ``<p>:RES?`` reports the resolution of the *measurement*, in the function's
    own unit.  Under dB or dBm scaling the displayed number is a logarithm of
    that measurement, and a resolution in volts says nothing about it — it is
    not convertible either, because the size of one step in dB depends on the
    reading.  Rather than invent a figure, the resolution is withdrawn, and
    the readout treats the reading exactly as it already treats CAP, FREQ,
    PER, TEMP, CONT and DIOD, where the instrument reports no resolution at
    all: every digit solid and no accuracy band (ARCHITECTURE.md section 3).
    """
    if not state:
        return None
    if scale_unit(state):
        return None
    value = state.get("resolution")
    if is_num(value) and float(value) > 0:
        return float(value)
    return None


def max_exponent(unit: str) -> Optional[int]:
    """Cap the SI prefix at 10^0 for volts and amps.

    This meter reads to 1000 V and 3 A, so it must never display kV or kA:
    the 1000 V range reads ``231.4567 V``.
    """
    return 0 if unit in ("V", "A") else None


def si_step(magnitude: float, max_exp: Optional[int]) -> Tuple[float, str]:
    limit = math.inf if max_exp is None else 10.0 ** max_exp
    for mult, prefix in SI_STEPS:
        if mult <= limit and magnitude >= mult:
            return mult, prefix
    return SI_STEPS[-1]


def trim_number(value: float, sig: int) -> str:
    """Fixed-point with *sig* significant figures, trailing zeros removed."""
    magnitude = abs(value)
    if magnitude >= 1000:
        decimals = 0
    elif magnitude >= 100:
        decimals = max(0, sig - 3)
    elif magnitude >= 10:
        decimals = max(0, sig - 2)
    elif magnitude >= 1:
        decimals = max(0, sig - 1)
    else:
        decimals = sig
    text = f"{value:.{min(decimals, 12)}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def fmt_si(
    value: Any,
    unit: str = "",
    sig: int = 5,
    no_prefix: bool = False,
    max_exp: Optional[int] = None,
) -> str:
    """Format *value* with an SI prefix; ``None`` renders as an em dash."""
    if not is_num(value):
        return "—"
    value = float(value)
    if value == 0.0:
        return "0 " + unit if unit else "0"
    if no_prefix:
        text = trim_number(value, sig)
        return f"{text} {unit}" if unit else text
    mult, prefix = si_step(abs(value), max_exp)
    text = trim_number(value / mult, sig)
    suffix = prefix + unit
    return f"{text} {suffix}" if suffix else text


def fmt_state(value: Any, state: Optional[Dict[str, Any]], sig: int = 5) -> str:
    """Format a measurement in the reading's own unit."""
    unit = unit_text(state)
    return fmt_si(value, unit, sig, unit_no_prefix(state), max_exponent(unit))


def common_si(
    values: List[Any], state: Optional[Dict[str, Any]]
) -> Tuple[float, str]:
    """One SI step for a group of related numbers.

    Statistics read as a set: scaling each of avg/min/max/sdev to its own
    prefix makes a 3.5 mV spread next to a -67 uV mean look unrelated.  The
    group takes the prefix of its largest member.
    """
    unit = unit_text(state)
    if unit_no_prefix(state):
        return 1.0, unit
    magnitudes = [abs(float(v)) for v in values if is_num(v) and v != 0]
    if not magnitudes:
        return 1.0, unit
    mult, prefix = si_step(max(magnitudes), max_exponent(unit))
    return mult, prefix + unit


def fmt_scaled(value: Any, mult: float, label: str, sig: int = 7) -> str:
    """Format *value* against a prefix already chosen by :func:`common_si`."""
    if not is_num(value):
        return "—"
    text = trim_number(float(value) / mult, sig)
    return f"{text} {label}" if label else text


def range_unit(state: Optional[Dict[str, Any]]) -> str:
    """The unit of the :RANG node — volts for FREQ and PER.

    Never dB: the range is a property of the measurement, not of the scaling
    applied to it, so it stays in the function's own unit even when the
    reading is being displayed in dB.
    """
    if state and state.get("func") in ("FREQ", "PER"):
        return "V"
    return base_unit(state)


def fmt_range(value: Any, state: Optional[Dict[str, Any]]) -> str:
    unit = range_unit(state)
    return fmt_si(value, unit, 4, False, max_exponent(unit))


def fmt_seconds(value: Any, sig: int = 4) -> str:
    return fmt_si(value, "s", sig)


def trim_float(value: Any) -> str:
    """A plain editable rendering of a float for a text field."""
    if not is_num(value):
        return ""
    text = repr(float(value))
    if text.endswith(".0"):
        text = text[:-2]
    return text


# ------------------------------------------------------------------ captions

_TEMP_NODES = {
    "temp_type": "TEMP:TRAN:TYPE",
    "temp_unit": "UNIT:TEMP",
    "rtd_res": "TEMP:TRAN:RTD:RES",
    "therm_type": "TEMP:TRAN:THER:TYPE",
}

_FIXED_NODES = {
    "source": "TRIG:SOUR",
    "delay": "TRIG:DEL",
    "delay_auto": "TRIG:DEL:AUTO",
    "count": "TRIG:COUN",
    "samples": "SAMP:COUN",
    "slope": "TRIG:SLOP",
    "scale_on": "CALC:SCAL:STAT",
    "scale_func": "CALC:SCAL:FUNC",
    "db_ref": "CALC:SCAL:DB:REF",
    "dbm_ref": "CALC:SCAL:DBM:REF",
    "stats_on": "CALC:AVER:STAT",
    "limit_on": "CALC:LIM:STAT",
    "limit_low": "CALC:LIM:LOW",
    "limit_high": "CALC:LIM:UPP",
    "hist_on": "CALC:TRAN:HIST:STAT",
    "hist_points": "CALC:TRAN:HIST:POIN",
    "hist_auto": "CALC:TRAN:HIST:RANG:AUTO",
    "hist_low": "CALC:TRAN:HIST:RANG:LOW",
    "hist_high": "CALC:TRAN:HIST:RANG:UPP",
    "view": "DISP:VIEW",
    "display_on": "DISP",
    "display_text": "DISP:TEXT",
}


def node_for(func: Optional[str], what: str) -> str:
    """The SCPI node a control writes, captioned beneath it.

    Regenerated per function, so selecting 4W relabels RANGE to ``FRES:RANG``.
    """
    if what in _FIXED_NODES:
        return _FIXED_NODES[what]
    if not func:
        return ""
    if what in _TEMP_NODES:
        return _TEMP_NODES[what]
    freq_like = func in ("FREQ", "PER")
    if what == "range":
        return "FREQ:VOLT:RANG" if freq_like else f"{func}:RANG"
    if what == "range_auto":
        return "FREQ:VOLT:RANG:AUTO" if freq_like else f"{func}:RANG:AUTO"
    if what == "nplc":
        return f"{func}:NPLC"
    if what == "azero":
        return f"{func}:ZERO:AUTO"
    if what == "aperture":
        return "PER:APER" if func == "PER" else "FREQ:APER"
    if what == "band":
        return f"{func}:BAND"
    if what == "impedance":
        return f"{func}:IMP:AUTO"
    if what == "null_on":
        return f"{func}:NULL:STAT"
    if what == "null_value":
        return f"{func}:NULL:VAL"
    if what == "null_auto":
        return f"{func}:NULL:VAL:AUTO"
    if what == "resolution":
        return f"{func}:RES?"
    return ""
