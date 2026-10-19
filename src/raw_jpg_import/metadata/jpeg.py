"""JPEG APP1 Exif locator: walks the marker segments up to the start of the image data."""

from __future__ import annotations

from collections.abc import Iterator

from ._io import ParseError, RandomAccess
from .tiff import ExifInfo, parse_tiff

MAX_SCAN = 1024 * 1024
MAX_FILL = 64

_SOI = b"\xff\xd8"
_APP1 = 0xE1
_SOS = 0xDA
_EOI = 0xD9
_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})   # TEM and RST0-7 have no length


def is_jpeg(head: bytes) -> bool:
    return head[:3] == b"\xff\xd8\xff"


def iter_exif_tiffs(ra: RandomAccess) -> Iterator[tuple[int, int]]:
    """Yield ``(offset, length)`` of the TIFF data in each ``Exif`` APP1 segment."""
    if ra.read_upto(0, 2) != _SOI:
        return
    pos = 2
    limit = min(ra.size, MAX_SCAN)
    while pos + 4 <= limit:
        if ra.read_at(pos, 1) != b"\xff":
            return                              # not at a marker: corrupt
        fill = 0
        while ra.read_at(pos + 1, 1) == b"\xff":   # fill bytes before the marker code
            pos += 1
            fill += 1
            if fill > MAX_FILL:
                return
        marker = ra.read_at(pos + 1, 1)[0]
        if marker in (_SOS, _EOI):
            return
        if marker in _STANDALONE:
            pos += 2
            continue
        length = ra.u16(pos + 2)
        if length < 2:
            return
        payload, size = pos + 4, length - 2
        if marker == _APP1 and size > 6 and ra.read_upto(payload, 5) == b"Exif\x00":
            yield payload + 6, min(size - 6, ra.size - payload - 6)
        pos += 2 + length


def parse_jpeg(ra: RandomAccess) -> ExifInfo | None:
    """Capture tags from the first readable Exif APP1 segment, or ``None``."""
    try:
        for start, length in iter_exif_tiffs(ra):
            try:
                info = parse_tiff(ra.window(start, length))
            except ParseError:
                continue
            if info is not None:
                return info
    except ParseError:
        pass
    return None
