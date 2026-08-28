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

"""Render the application icon set from the project's own design tokens.

Run once when the artwork changes; the generated files are committed so the
build scripts never need Pillow on the build host to produce an icon:

    python packaging/make_icons.py

Produces
    packaging/icons/icon.png   1024x1024 master, used by Debian and macOS
    packaging/icons/icon.ico   multi-resolution Windows icon
    packaging/icons/icon-256.png, icon-128.png, ... Debian hicolor sizes

macOS ``.icns`` is *not* generated here.  It is built on the macOS runner by
``build_macos.sh`` with ``sips`` + ``iconutil`` from ``icon.png``, because those
are the tools that produce a bundle Finder is guaranteed to accept.

The mark is a multimeter DC-volts symbol -- ``V`` under a solid bar over a
dashed bar -- in phosphor on the instrument bezel, which is what the readout
itself looks like.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "icons")
FONT = os.path.join(ROOT, "app", "ui", "fonts", "MartianMono-Regular.ttf")

# app/ui/theme.py tokens, kept in step by hand -- this script must not import
# the application (it would drag in PySide6 for no reason).
INK = (14, 20, 25, 255)
GLASS = (10, 15, 19, 255)
GLASS_EDGE = (31, 44, 54, 255)
PHOSPHOR = (242, 239, 230, 255)
SIGNAL = (79, 195, 232, 255)

S = 1024  # master size; everything below is expressed as a fraction of it


def _rounded(draw: ImageDraw.ImageDraw, box, radius, **kw) -> None:
    draw.rounded_rectangle(box, radius=radius, **kw)


def render(size: int = S) -> Image.Image:
    k = size / 1024.0
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # bezel
    _rounded(draw, (0, 0, size - 1, size - 1), int(190 * k), fill=INK)
    _rounded(
        draw,
        (int(18 * k), int(18 * k), size - 1 - int(18 * k), size - 1 - int(18 * k)),
        int(174 * k),
        outline=GLASS_EDGE,
        width=max(1, int(8 * k)),
    )

    # recessed glass well
    _rounded(
        draw,
        (int(96 * k), int(150 * k), size - 1 - int(96 * k), size - 1 - int(150 * k)),
        int(72 * k),
        fill=GLASS,
        outline=GLASS_EDGE,
        width=max(1, int(6 * k)),
    )

    # the DC-volts mark: V with a solid bar over a dashed bar above it
    font = ImageFont.truetype(FONT, int(430 * k))
    text = "V"
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top + int(64 * k)),
        text,
        font=font,
        fill=PHOSPHOR,
    )

    bar_w = int(300 * k)
    x0 = (size - bar_w) // 2
    y = int(300 * k)
    h = max(1, int(22 * k))
    draw.rounded_rectangle((x0, y, x0 + bar_w, y + h), radius=h // 2, fill=SIGNAL)

    # dashed lower bar
    dash, gap = max(2, int(58 * k)), max(1, int(38 * k))
    x = x0
    y2 = y + int(58 * k)
    while x < x0 + bar_w:
        draw.rounded_rectangle(
            (x, y2, min(x + dash, x0 + bar_w), y2 + h), radius=h // 2, fill=SIGNAL
        )
        x += dash + gap
    return image


def main() -> int:
    if not os.path.isfile(FONT):
        sys.stderr.write(f"missing font: {FONT}\n")
        return 1
    os.makedirs(OUT, exist_ok=True)

    master = render(S)
    master.save(os.path.join(OUT, "icon.png"))

    # Windows .ico -- render each size natively rather than downscaling one
    # bitmap, so the 16px entry is still legible.
    ico_sizes = (256, 128, 64, 48, 32, 24, 16)
    frames = [render(n) for n in ico_sizes]
    frames[0].save(
        os.path.join(OUT, "icon.ico"),
        format="ICO",
        sizes=[(n, n) for n in ico_sizes],
        append_images=frames[1:],
    )

    for n in (512, 256, 128, 64, 48, 32, 16):
        render(n).save(os.path.join(OUT, f"icon-{n}.png"))

    print(f"wrote icon.png, icon.ico and 7 sized PNGs into {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
