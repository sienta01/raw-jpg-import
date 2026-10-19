"""Byte-exact JPEG builder: SOI, APP0, APP1 (Exif), tables, SOS, a filler body and EOI."""

from __future__ import annotations

import random
import struct
from datetime import datetime

from fakes.tiff import make_tiff

DEFAULT_SIZE = 64 * 1024

_JFIF = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
_DQT = b"\x00" + bytes(range(1, 65))
_SOF0 = b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"      # 8-bit, 1x1, one component
_DHT = b"\x00" + b"\x01" + b"\x00" * 15 + b"\x00"     # one 1-bit code (never decoded)
_SOS = b"\x01\x01\x00\x00\x3f\x00"


def segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


def seeded_bytes(seed: str, n: int, *, no_ff: bool = False) -> bytes:
    """``n`` pseudo-random bytes that depend only on ``seed``."""
    data = random.Random(seed).randbytes(max(0, n))
    return data.replace(b"\xff", b"\xfe") if no_ff else data


def size_for(name: str, size: int | None, base: int = DEFAULT_SIZE) -> int:
    """``size`` if given, else about ``base`` with a jitter seeded by ``name`` (unique sizes)."""
    return size if size is not None else base + random.Random("size:" + name).randrange(8192)


def make_jpeg(dto: datetime | str | None, *, bo: str = "<", app0: bool = True,
              exif: bool = True, fill_bytes: int = 0, name: str = "DSC_0001.JPG",
              size: int | None = None, body_size: int | None = None, **tiff_kw) -> bytes:
    """A JPEG whose APP1 Exif segment holds ``make_tiff(dto, bo=bo, **tiff_kw)``.

    ``fill_bytes`` 0xFF fill bytes are put before the APP1 marker. The total size is
    ``size`` (default about 64 KiB, seeded by ``name``) unless ``body_size`` fixes the
    length of the entropy-coded filler instead.
    """
    head = b"\xff\xd8"
    if app0:
        head += segment(0xE0, _JFIF)
    if exif:
        head += b"\xff" * fill_bytes + segment(0xE1, b"Exif\x00\x00" + make_tiff(dto, bo=bo,
                                                                                 **tiff_kw))
    head += segment(0xDB, _DQT) + segment(0xC0, _SOF0) + segment(0xC4, _DHT)
    head += segment(0xDA, _SOS)
    if body_size is None:
        body_size = max(16, size_for(name, size) - len(head) - 2)
    return head + seeded_bytes(name, body_size, no_ff=True) + b"\xff\xd9"
