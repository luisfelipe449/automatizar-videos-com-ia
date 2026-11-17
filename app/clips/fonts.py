"""Which font can draw a streamer's name.

drawtext has no font fallback: a character missing from the font renders as
an empty box. Bebas Neue covers Latin (accents and Latin Extended included)
but not Cyrillic, Greek or CJK, which Twitch display names often use.
`credit_font` keeps the brand font whenever it covers the whole name and
switches to a Windows system font otherwise.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

from app.config import ASSETS_DIR

BRAND_FONT = ASSETS_DIR / "fonts" / "BebasNeue-Regular.ttf"
# Microsoft YaHei covers Latin, Cyrillic, Greek and CJK; it has no Hangul,
# which Malgun Gothic has. FFmpeg reads face 0 of a .ttc collection.
CJK_FONT = Path("C:/Windows/Fonts/msyh.ttc")
HANGUL_FONT = Path("C:/Windows/Fonts/malgun.ttf")


@lru_cache(maxsize=8)
def _codepoints(path: Path) -> frozenset[int]:
    """Code points mapped by the font's Unicode BMP cmap (format 4).

    Reads a bare .ttf/.otf only. Characters outside the BMP (emoji) are never
    reported as covered.
    """
    data = path.read_bytes()
    (num_tables,) = struct.unpack(">H", data[4:6])
    cmap = None
    for i in range(num_tables):
        rec = 12 + 16 * i
        if data[rec:rec + 4] == b"cmap":
            (cmap,) = struct.unpack(">I", data[rec + 8:rec + 12])
            break
    if cmap is None:
        return frozenset()

    (n_sub,) = struct.unpack(">H", data[cmap + 2:cmap + 4])
    points: set[int] = set()
    for i in range(n_sub):
        platform, encoding, offset = struct.unpack(">HHI", data[cmap + 4 + 8 * i:cmap + 12 + 8 * i])
        if (platform, encoding) not in ((0, 3), (3, 1)):
            continue
        sub = cmap + offset
        (fmt,) = struct.unpack(">H", data[sub:sub + 2])
        if fmt != 4:
            continue
        (seg_x2,) = struct.unpack(">H", data[sub + 6:sub + 8])
        segs = seg_x2 // 2
        ends = struct.unpack(f">{segs}H", data[sub + 14:sub + 14 + seg_x2])
        starts = struct.unpack(f">{segs}H", data[sub + 16 + seg_x2:sub + 16 + 2 * seg_x2])
        for start, end in zip(starts, ends):
            if start != 0xFFFF:
                points.update(range(start, end + 1))
    return frozenset(points)


def covers(font: Path, text: str) -> bool:
    points = _codepoints(Path(font))
    return all(ord(ch) in points for ch in text if not ch.isspace())


def _has_hangul(text: str) -> bool:
    return any(0xAC00 <= ord(ch) <= 0xD7A3 or 0x1100 <= ord(ch) <= 0x11FF for ch in text)


def credit_font(
    name: str,
    *,
    primary: Path = BRAND_FONT,
    cjk: Path = CJK_FONT,
    hangul: Path = HANGUL_FONT,
) -> Path:
    """The brand font if it covers `name`, else a system font that does.

    Falls back to the brand font when the system font is missing (non-Windows
    machine), accepting boxes over a failed render.
    """
    if covers(primary, name):
        return primary
    fallback = hangul if _has_hangul(name) else cjk
    return fallback if fallback.is_file() else primary
