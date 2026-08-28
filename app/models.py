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

"""The one place that knows which instrument models this application is for.

Everything the application sends was established by probing a physical
**34461A** (SPEC.md section 1), and two things are keyed to that specific model
rather than to the Truevolt family in general:

* ``app/specs.py`` — the function list, per-function sense nodes and the range
  enumeration.  A 34460A has a different function and range set.
* the never-send list in SPEC.md section 2.2, enforced in ``app/scpi.py``.  It
  is model-specific in *both* directions: a 34465A/34470A genuinely supports
  several of the commands that hang a 34461A, and a 34460A does not support
  some that a 34461A does.

So the application does not adapt its command set to whatever answers ``*IDN?``.
It reports what it found, says plainly that the behaviour is unverified for it,
and lets the user decide — see :func:`support_for`.

**Adding a model later is additive, and this is the seam.**  Add a
:class:`ModelSupport` entry to :data:`SUPPORTED` and, in the same change, the
verified function/range metadata to ``app/specs.py`` and the verified
never-send list to ``app/scpi.py``.  Nothing else in the application matches on
a model string, so nothing else has to move.

**Do not add an entry here from a datasheet.**  Every fact about the 34461A in
this repository came from probing the instrument, including the discovery that
querying autozero on 4-wire resistance hangs the socket — which no datasheet
says.  A new model needs the same hardware verification before it is listed as
supported, because the cost of being wrong is a wedged bench instrument.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

__all__ = [
    "PRIMARY_MODEL",
    "SUPPORTED",
    "ModelSupport",
    "support_for",
    "is_verified",
    "unverified_warning",
]


@dataclass(frozen=True)
class ModelSupport:
    """What is known about one instrument model.

    ``verified`` is True only when this repository's SCPI command set — the
    supported list, the never-send list and the range metadata — was probed
    against that physical model.  It is never inferred from a family name.
    """

    model: str
    verified: bool
    description: str


#: The model every fact in SPEC.md was measured against.  The window title,
#: the About box and the connection guard all read it from here.
PRIMARY_MODEL = "34461A"

#: Models whose command set has been hardware-verified for this application.
#: One entry today, by design; see the module docstring for how to add another.
SUPPORTED: Dict[str, ModelSupport] = {
    PRIMARY_MODEL: ModelSupport(
        model=PRIMARY_MODEL,
        verified=True,
        description="Keysight 34461A 6.5-digit Truevolt DMM",
    ),
}


def _normalise(model: str) -> str:
    """The bare model token from an ``*IDN?`` field.

    ``*IDN?`` on this unit answers ``Keysight Technologies,34461A,MY12345678,
    A.03.03-...``; the caller passes the second field, but be tolerant of
    surrounding whitespace and of case.
    """
    return (model or "").strip().upper()


def support_for(model: str) -> Optional[ModelSupport]:
    """The verified support record for *model*, or None if there is none."""
    return SUPPORTED.get(_normalise(model))


def is_verified(model: str) -> bool:
    """True when this application's command set was probed against *model*."""
    return support_for(model) is not None


def unverified_warning(model: str) -> str:
    """The exact text shown when an unverified model answers ``*IDN?``.

    Written once, here, so the dialog, the banner and the log all say the same
    thing.  It names the model found rather than describing it vaguely: the
    difference between a 34461A and a 34465A is the difference between a
    command that answers and one that hangs the socket.
    """
    found = (model or "").strip() or "an instrument that did not name its model"
    verified = ", ".join(sorted(SUPPORTED))
    return (
        f"This instrument reports itself as {found}, not {verified}.\n\n"
        f"Every SCPI command this application sends was verified against a "
        f"{PRIMARY_MODEL} on real hardware, including the list of commands "
        f"that must never be sent because they make the instrument stop "
        f"answering. Those lists are model-specific: a 34465A or 34470A "
        f"supports several commands that hang a {PRIMARY_MODEL}, and a 34460A "
        f"has a different function and range set.\n\n"
        f"Nothing here has been verified against {found}. The functions, "
        f"ranges and safety limits may be wrong for it, and a command that is "
        f"safe on a {PRIMARY_MODEL} may leave it unresponsive until it is "
        f"power-cycled.\n\n"
        f"You can go ahead if you accept that risk."
    )
