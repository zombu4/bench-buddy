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

"""Convert the 34461A's screen dump into a PNG.

``HCOP:SDUM:DATA:FORM BMP`` is the only format worth using: it answers in about
0.16 s and yields 277494 bytes for the 480x289 panel, while the instrument's own
PNG encoder takes 2.56 s.  The bitmap is 16 bpp BI_RGB — which by the Windows
BITMAPINFOHEADER definition means X1R5G5B5 — stored top-down (negative height),
pixel data at offset 54, row stride 960 bytes:

    480 * 289 * 2 + 54 == 277494
"""

from __future__ import annotations

import io
import struct
from typing import Tuple

import numpy as np
from PIL import Image

BI_RGB = 0
BI_BITFIELDS = 3


class ScreenDecodeError(Exception):
    """The screen dump was not the bitmap this instrument is documented to send."""


def _mask_to_channel(values: np.ndarray, mask: int) -> np.ndarray:
    """Extract a colour channel described by *mask* and scale it to 0..255."""
    if mask == 0:
        return np.zeros(values.shape, dtype=np.uint8)
    shift = (mask & -mask).bit_length() - 1
    width = bin(mask >> shift).count("1")
    raw = (values >> shift) & ((1 << width) - 1)
    if width == 8:
        return raw.astype(np.uint8)
    full = (1 << width) - 1
    # Replicate the high bits downward so full-scale maps exactly to 255.
    return ((raw.astype(np.uint32) * 255 + full // 2) // full).astype(np.uint8)


def decode_bmp(data: bytes) -> Tuple[np.ndarray, int, int]:
    """Decode a 16 bpp BMP into an (H, W, 3) uint8 RGB array."""
    if len(data) < 54:
        raise ScreenDecodeError(f"screen dump is only {len(data)} bytes")
    if data[:2] != b"BM":
        raise ScreenDecodeError(f"not a BMP: magic {data[:2]!r}")

    file_size, _, _, pixel_offset = struct.unpack_from("<IHHI", data, 2)
    header_size = struct.unpack_from("<I", data, 14)[0]
    if header_size < 40:
        raise ScreenDecodeError(f"unsupported DIB header size {header_size}")
    width, height, planes, bpp, compression = struct.unpack_from("<iiHHI", data, 18)

    if planes != 1:
        raise ScreenDecodeError(f"unsupported plane count {planes}")
    if bpp != 16:
        raise ScreenDecodeError(f"expected 16 bpp, got {bpp}")
    if compression not in (BI_RGB, BI_BITFIELDS):
        raise ScreenDecodeError(f"unsupported BMP compression {compression}")

    top_down = height < 0
    abs_height = abs(height)
    if width <= 0 or abs_height == 0:
        raise ScreenDecodeError(f"nonsensical bitmap size {width}x{height}")

    if compression == BI_BITFIELDS:
        if header_size >= 56:
            r_mask, g_mask, b_mask = struct.unpack_from("<III", data, 14 + 40)
        elif len(data) >= 54 + 12:
            r_mask, g_mask, b_mask = struct.unpack_from("<III", data, 54)
        else:
            raise ScreenDecodeError("BI_BITFIELDS bitmap without colour masks")
    else:
        # BI_RGB at 16 bpp is X1R5G5B5.
        r_mask, g_mask, b_mask = 0x7C00, 0x03E0, 0x001F

    stride = ((width * bpp + 31) // 32) * 4
    needed = pixel_offset + stride * abs_height
    if len(data) < needed:
        raise ScreenDecodeError(
            f"truncated bitmap: {len(data)} bytes, need {needed} "
            f"(offset {pixel_offset}, stride {stride}, height {abs_height}, "
            f"file_size field {file_size})"
        )

    rows = np.frombuffer(
        data, dtype=np.uint8, count=stride * abs_height, offset=pixel_offset
    ).reshape(abs_height, stride)
    pixels = rows[:, : width * 2].reshape(abs_height, width, 2)
    values = pixels[:, :, 0].astype(np.uint16) | (
        pixels[:, :, 1].astype(np.uint16) << 8
    )

    rgb = np.empty((abs_height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = _mask_to_channel(values, r_mask)
    rgb[:, :, 1] = _mask_to_channel(values, g_mask)
    rgb[:, :, 2] = _mask_to_channel(values, b_mask)

    if not top_down:
        rgb = rgb[::-1]
    return rgb, width, abs_height


def bmp_to_png(data: bytes, scale: int = 1) -> bytes:
    """Decode the instrument's BMP screen dump and re-encode it as a PNG."""
    rgb, width, height = decode_bmp(data)
    image = Image.fromarray(rgb, mode="RGB")
    if scale > 1:
        image = image.resize((width * scale, height * scale), Image.NEAREST)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=1)
    return buffer.getvalue()
