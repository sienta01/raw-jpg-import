"""Byte-exact TIFF/Exif builder: IFD0 plus an Exif IFD with chosen dates."""

from __future__ import annotations

import struct
from datetime import datetime

ASCII = 2
LONG = 4

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_DTO = 0x9003
TAG_DTD = 0x9004
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_SUBSEC_ORIGINAL = 0x9291

Entry = tuple[int, int, int, bytes]     # tag, type, count, value bytes


def exif_date(value: datetime | str) -> str:
    """``"2026:01:23 23:02:15"``; a string is passed through (e.g. ``"0000:00:00 00:00:00"``)."""
    if isinstance(value, str):
        return value
    return (f"{value.year:04d}:{value.month:02d}:{value.day:02d} "
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}")


def ascii_entry(tag: int, text: str) -> Entry:
    raw = text.encode("ascii") + b"\x00"
    return tag, ASCII, len(raw), raw


def build_ifd(entries: list[Entry], at: int, bo: str, next_ifd: int = 0) -> bytes:
    """An IFD placed at offset ``at`` of the TIFF data, followed by its out-of-line values.

    Values of 4 bytes or fewer are stored inline in the entry, as the spec requires.
    """
    entries = sorted(entries)
    data_at = at + 2 + 12 * len(entries) + 4
    head = struct.pack(bo + "H", len(entries))
    data = b""
    for tag, typ, count, value in entries:
        if len(value) <= 4:
            field = value.ljust(4, b"\x00")
        else:
            field = struct.pack(bo + "I", data_at + len(data))
            data += value + (b"\x00" if len(value) % 2 else b"")
        head += struct.pack(bo + "HHI", tag, typ, count) + field
    return head + struct.pack(bo + "I", next_ifd) + data


def make_tiff(dto: datetime | str | None, *, bo: str = "<", magic: int = 42,
              make: str | None = "NIKON CORPORATION", model: str | None = "NIKON Z f",
              subsec: str | None = None, offset: str | None = None,
              exif_in_ifd0: bool = False, extra_padding: int = 0,
              dtd: datetime | str | None = None,
              ifd0_datetime: datetime | str | None = None) -> bytes:
    """TIFF bytes with IFD0 (Make, Model, DateTime) and an Exif IFD (dates, sub-seconds).

    ``exif_in_ifd0`` puts the Exif tags straight into IFD0 (as in CR3's CMT2).
    ``extra_padding`` zero bytes are appended after the structures.
    """
    if bo not in ("<", ">"):
        raise ValueError("bo must be '<' or '>'")
    ifd0: list[Entry] = []
    if make is not None:
        ifd0.append(ascii_entry(TAG_MAKE, make))
    if model is not None:
        ifd0.append(ascii_entry(TAG_MODEL, model))
    if ifd0_datetime is not None:
        ifd0.append(ascii_entry(TAG_DATETIME, exif_date(ifd0_datetime)))
    exif: list[Entry] = []
    if dto is not None:
        exif.append(ascii_entry(TAG_DTO, exif_date(dto)))
    if dtd is not None:
        exif.append(ascii_entry(TAG_DTD, exif_date(dtd)))
    if offset is not None:
        exif.append(ascii_entry(TAG_OFFSET_TIME_ORIGINAL, offset))
    if subsec is not None:
        exif.append(ascii_entry(TAG_SUBSEC_ORIGINAL, subsec))

    header = (b"II" if bo == "<" else b"MM") + struct.pack(bo + "HI", magic, 8)
    if exif_in_ifd0:
        body = build_ifd(ifd0 + exif, 8, bo)
    else:
        # The pointer is stored inline, so IFD0's size does not depend on its value.
        probe = build_ifd([*ifd0, (TAG_EXIF_IFD, LONG, 1, b"\0" * 4)], 8, bo)
        exif_at = 8 + len(probe)
        pointer = (TAG_EXIF_IFD, LONG, 1, struct.pack(bo + "I", exif_at))
        body = build_ifd([*ifd0, pointer], 8, bo) + build_ifd(exif, exif_at, bo)
    return header + body + b"\x00" * extra_padding
