"""Fujifilm RAF: the capture date comes from the embedded JPEG preview's Exif."""

from __future__ import annotations

from ._io import RandomAccess
from .jpeg import parse_jpeg
from .tiff import ExifInfo

MAGIC = b"FUJIFILMCCD-RAW "
_JPEG_OFFSET_AT = 84
_JPEG_LENGTH_AT = 88


def is_raf(head: bytes) -> bool:
    return head[:16] == MAGIC


def parse_raf(ra: RandomAccess) -> ExifInfo | None:
    if not is_raf(ra.read_upto(0, 16)):
        return None
    offset = ra.u32(_JPEG_OFFSET_AT)
    length = ra.u32(_JPEG_LENGTH_AT)
    if offset < _JPEG_LENGTH_AT + 4 or length < 4:
        return None
    return parse_jpeg(ra.window(offset, min(length, ra.size - offset)))
