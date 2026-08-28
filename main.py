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

"""Entry point for the Bench Buddy desktop application.

    python main.py [--instrument HOST] [--transport auto|vxi11|socket]
                   [--finite-trigger-count] [--port 5025]

Every argument is optional.  With no ``--instrument`` and nothing saved from a
previous run, the application opens its connection dialog rather than guessing
at an address; saved instruments are also reachable from the Instruments menu
and the picker beside the Connect button.

This module is also the PyInstaller entry script, so it must stay importable
with no side effects beyond starting Qt.
"""

from __future__ import annotations

import os
import sys


def _ensure_package_on_path() -> None:
    """Make ``app`` importable when run as a script or from a frozen bundle."""
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)


def main() -> int:
    _ensure_package_on_path()
    from app.ui.main import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
