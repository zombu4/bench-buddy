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

"""Function metadata for the 34461A.

Twelve measurement functions.  Each entry records the SCPI facts the rest of the
app needs: which ``CONF:`` command selects it, which sense prefix carries its
range/null nodes, and which of the optional sub-nodes actually exist for it.

Nothing here is a range table — ranges are enumerated from the instrument at
runtime with ``<prefix>:RANG? MIN`` / ``MAX`` (SPEC.md section 2.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

NPLC_OPTIONS: List[float] = [0.02, 0.2, 1.0, 10.0, 100.0]
APERTURE_OPTIONS: List[float] = [0.001, 0.01, 0.1, 1.0]
BAND_OPTIONS: List[int] = [3, 20, 200]
AZERO_OPTIONS: List[str] = ["ON", "OFF", "ONCE"]
TRIG_SOURCES: List[str] = ["IMM", "BUS", "EXT"]
TRIG_SLOPES: List[str] = ["POS", "NEG"]
# What may be *written* to CALC:SCAL:FUNC.  The query answers NULL|DB|DBM, but
# this firmware refuses `CALC:SCAL:FUNC NULL` with -224,"Illegal parameter
# value" (SPEC.md section 2.1, measured twice with scaling both on and off), so
# offering it could only ever produce an error.  Nulling is done through the
# per-function `<p>:NULL:*` nodes, which are the only null path this firmware
# implements.  ``instrument._apply_math_field`` still refuses NULL explicitly,
# as defence for any caller that does not read this list.
SCALE_FUNCS: List[str] = ["DB", "DBM"]
DISPLAY_VIEWS: List[str] = ["NUM", "TCH", "HIST", "MET"]
TEMP_PROBE_TYPES: List[str] = ["FRTD", "RTD", "FTH", "THER"]
TEMP_UNITS: List[str] = ["C", "F", "K"]
THERMISTOR_TYPES: List[str] = ["5000"]

# The widest span the decade enumeration rule of SPEC.md section 2.4 may cover.
MAX_RANGE_DECADES: int = 32


@dataclass(frozen=True)
class FunctionSpec:
    """Everything the backend needs to drive one measurement function."""

    key: str  # e.g. "VOLT:DC" — the API identifier
    label: str  # e.g. "DC Voltage"
    short: str  # e.g. "DCV" — rotary-switch mnemonic
    unit: str  # e.g. "V"
    conf: str  # argument to CONF: (e.g. "VOLT:DC")
    sense: Optional[str]  # sense prefix carrying NULL etc., None for CONT/DIOD
    range_node: Optional[str]  # prefix owning :RANG (FREQ/PER borrow FREQ:VOLT)
    range_unit: str  # unit of the range value (FREQ/PER range is in volts)
    nplc: bool
    azero: bool
    impedance: bool
    band: bool
    aperture_node: Optional[str]  # prefix owning :APER when settable
    has_resolution: bool  # whether <prefix>:RES? may be queried
    temperature: bool = False

    @property
    def has_range(self) -> bool:
        return self.range_node is not None

    @property
    def has_null(self) -> bool:
        return self.sense is not None


def _spec(**kwargs) -> FunctionSpec:
    kwargs.setdefault("range_node", None)
    kwargs.setdefault("range_unit", "")
    kwargs.setdefault("nplc", False)
    kwargs.setdefault("azero", False)
    kwargs.setdefault("impedance", False)
    kwargs.setdefault("band", False)
    kwargs.setdefault("aperture_node", None)
    kwargs.setdefault("has_resolution", False)
    return FunctionSpec(**kwargs)


FUNCS: Dict[str, FunctionSpec] = {
    "VOLT:DC": _spec(
        key="VOLT:DC",
        label="DC Voltage",
        short="DCV",
        unit="V",
        conf="VOLT:DC",
        sense="VOLT:DC",
        range_node="VOLT:DC",
        range_unit="V",
        nplc=True,
        azero=True,
        impedance=True,
        has_resolution=True,
    ),
    "VOLT:AC": _spec(
        key="VOLT:AC",
        label="AC Voltage",
        short="ACV",
        unit="V",
        conf="VOLT:AC",
        sense="VOLT:AC",
        range_node="VOLT:AC",
        range_unit="V",
        band=True,
        has_resolution=True,
    ),
    "CURR:DC": _spec(
        key="CURR:DC",
        label="DC Current",
        short="DCI",
        unit="A",
        conf="CURR:DC",
        sense="CURR:DC",
        range_node="CURR:DC",
        range_unit="A",
        nplc=True,
        azero=True,
        has_resolution=True,
    ),
    "CURR:AC": _spec(
        key="CURR:AC",
        label="AC Current",
        short="ACI",
        unit="A",
        conf="CURR:AC",
        sense="CURR:AC",
        range_node="CURR:AC",
        range_unit="A",
        band=True,
        has_resolution=True,
    ),
    "RES": _spec(
        key="RES",
        label="2-Wire Resistance",
        short="2W",
        unit="ohm",
        conf="RES",
        sense="RES",
        range_node="RES",
        range_unit="ohm",
        nplc=True,
        azero=True,
        has_resolution=True,
    ),
    "FRES": _spec(
        key="FRES",
        label="4-Wire Resistance",
        short="4W",
        unit="ohm",
        conf="FRES",
        sense="FRES",
        range_node="FRES",
        range_unit="ohm",
        nplc=True,
        # SPEC.md section 8 lists azero for FRES, but section 2.1 does not and
        # the instrument agrees with 2.1: FRES:ZERO:AUTO? hangs the socket.
        azero=False,
        has_resolution=True,
    ),
    "FREQ": _spec(
        key="FREQ",
        label="Frequency",
        short="FREQ",
        unit="Hz",
        conf="FREQ",
        sense="FREQ",
        range_node="FREQ:VOLT",
        range_unit="V",
        aperture_node="FREQ",
    ),
    "PER": _spec(
        key="PER",
        label="Period",
        short="PER",
        unit="s",
        conf="PER",
        sense="PER",
        range_node="FREQ:VOLT",
        range_unit="V",
        aperture_node="PER",
    ),
    "CAP": _spec(
        key="CAP",
        label="Capacitance",
        short="CAP",
        unit="F",
        conf="CAP",
        sense="CAP",
        range_node="CAP",
        range_unit="F",
    ),
    "CONT": _spec(
        key="CONT",
        label="Continuity",
        short="CONT",
        unit="ohm",
        conf="CONT",
        sense=None,
    ),
    "DIOD": _spec(
        key="DIOD",
        label="Diode",
        short="DIODE",
        unit="V",
        conf="DIOD",
        sense=None,
    ),
    "TEMP": _spec(
        key="TEMP",
        label="Temperature",
        short="TEMP",
        unit="deg",
        conf="TEMP",
        sense="TEMP",
        nplc=True,
        azero=True,
        temperature=True,
    ),
}

# Distinct prefixes that own a :RANG node, for startup enumeration.
RANGE_NODES: List[str] = []
for _spec_entry in FUNCS.values():
    if _spec_entry.range_node and _spec_entry.range_node not in RANGE_NODES:
        RANGE_NODES.append(_spec_entry.range_node)

# ``SENS:FUNC?`` answers with a short form; map it back to our keys.
SENSE_FUNC_TO_KEY: Dict[str, str] = {
    "VOLT": "VOLT:DC",
    "VOLT:DC": "VOLT:DC",
    "VOLT:AC": "VOLT:AC",
    "CURR": "CURR:DC",
    "CURR:DC": "CURR:DC",
    "CURR:AC": "CURR:AC",
    "RES": "RES",
    "FRES": "FRES",
    "FREQ": "FREQ",
    "PER": "PER",
    "CAP": "CAP",
    "CONT": "CONT",
    "DIOD": "DIOD",
    "TEMP": "TEMP",
}


# The long forms of every mnemonic that can appear in a SENS:FUNC? answer.
# This unit replies with the short quoted form, but a long form must resolve to
# the same function rather than to a shorter mnemonic that happens to prefix it.
LONG_MNEMONICS: Dict[str, str] = {
    "VOLTAGE": "VOLT",
    "CURRENT": "CURR",
    "RESISTANCE": "RES",
    "FRESISTANCE": "FRES",
    "FREQUENCY": "FREQ",
    "PERIOD": "PER",
    "CAPACITANCE": "CAP",
    "CONTINUITY": "CONT",
    "DIODE": "DIOD",
    "TEMPERATURE": "TEMP",
}


def _shorten(node: str) -> str:
    """Reduce one SCPI mnemonic to the short form used by SENSE_FUNC_TO_KEY."""
    for long_form, short in LONG_MNEMONICS.items():
        # Accept any legal abbreviation between the short and the long form,
        # e.g. VOLT, VOLTA, VOLTAG, VOLTAGE all mean VOLT.
        if node.startswith(short) and long_form.startswith(node):
            return short
    return node


def resolve_sense_func(raw: str) -> Optional[str]:
    """Map a ``SENS:FUNC?`` / ``CONF?`` answer onto a FUNCS key."""
    token = raw.strip().strip('"').strip("'").upper()
    if not token:
        return None
    token = token.split(" ")[0].split(",")[0].strip()
    if token in SENSE_FUNC_TO_KEY:
        return SENSE_FUNC_TO_KEY[token]
    # Handle forms like "VOLTAGE:DC" or ":VOLT:DC": normalise every mnemonic in
    # the node to its short form before matching.
    token = ":".join(_shorten(node) for node in token.lstrip(":").split(":"))
    if token in SENSE_FUNC_TO_KEY:
        return SENSE_FUNC_TO_KEY[token]
    # Longest prefix first, so "VOLT:AC" is tested before "VOLT".
    for key in sorted(SENSE_FUNC_TO_KEY, key=len, reverse=True):
        if token.startswith(key):
            return SENSE_FUNC_TO_KEY[key]
    return None


def enumerate_ranges(minimum: float, maximum: float) -> List[float]:
    """Build a range list by decade from MIN up to MAX (SPEC.md section 2.4).

    e.g. CURR:DC MIN 1e-4, MAX 3 -> [1e-4, 1e-3, 1e-2, 1e-1, 1, 3]
    """
    if minimum <= 0 or maximum <= 0 or maximum < minimum:
        raise ValueError(f"nonsensical range bounds MIN={minimum} MAX={maximum}")
    values: List[float] = []
    value = minimum
    # Guard against runaway loops on unexpected bounds.  Reaching the cap means
    # the instrument reported bounds this rule cannot express, which SPEC.md
    # section 0 says to report rather than paper over with a short wrong list.
    for _ in range(MAX_RANGE_DECADES):
        if value >= maximum * (1 - 1e-9):
            break
        values.append(float(f"{value:.6g}"))
        value *= 10.0
    else:
        raise ValueError(
            f"range bounds MIN={minimum:g} MAX={maximum:g} span more than "
            f"{MAX_RANGE_DECADES} decades; the enumeration rule does not apply"
        )
    values.append(float(f"{maximum:.6g}"))
    return values
