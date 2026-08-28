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

"""Qt desktop shell for Bench Buddy.

Nothing in this package talks to the instrument directly.  Every exchange goes
through :mod:`app.ui.bridge`, which owns the single :class:`app.instrument.
Dmm34461A` on a dedicated ``QThread``.
"""

from __future__ import annotations

__all__ = ["main"]


def main() -> int:
    """Entry point; imported lazily so ``-m app.ui`` stays cheap to import."""
    from .main import main as _main

    return _main()
