"""TIFF IFD walker for capture dates (NEF, DNG, ARW, ORF, RW2, CR2, ... and embedded Exif)."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import datetime

from ._io import ParseError, RandomAccess

MAGICS = frozenset({42, 0x4F52, 0x5352, 0x55})   # TIFF, ORF ("RO", "RS"), RW2
MAX_ENTRIES = 512
MAX_IFDS = 6
MAX_ASCII = 256

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_DTO = 0x9003
TAG_DTD = 0x9004
TAG_SUBSEC_ORIGINAL = 0x9291

_ASCII_TAGS = frozenset({TAG_MAKE, TAG_MODEL, TAG_DATETIME, TAG_OFFSET_TIME_ORIGINAL,
                         TAG_DTO, TAG_DTD, TAG_SUBSEC_ORIGINAL})
# EXIF types whose bytes may hold text: BYTE, ASCII, UNDEFINED
_TEXT_TYPES = frozenset({1, 2, 7})

_DATE_RE = re.compile(r"^(\d{4})[:\-/](\d{2})[:\-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class ExifInfo:
    """The few tags the importer needs. Dates are naive camera wall-clock times."""

    dto: datetime | None = None          # 0x9003 DateTimeOriginal
    dtd: datetime | None = None          # 0x9004 DateTimeDigitized
    date_time: datetime | None = None    # 0x0132 DateTime (IFD0, "modified")
    subsec_ms: int | None = None         # 0x9291 SubSecTimeOriginal
    offset: str | None = None            # 0x9011 OffsetTimeOriginal, "+07:00"
    make: str | None = None
    model: str | None = None

    @property
    def best(self) -> datetime | None:
        """DateTimeOriginal, then DateTimeDigitized, then DateTime."""
        return self.dto or self.dtd or self.date_time

    def merged(self, other: ExifInfo | None) -> ExifInfo:
        """Field-wise ``self`` first, filling gaps from ``other``."""
        if other is None:
            return self
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        for name, value in values.items():
            if value is None:
                values[name] = getattr(other, name)
        return ExifInfo(**values)


def parse_exif_date(text: str) -> datetime | None:
    """``"2026:01:23 23:02:15"`` -> datetime; ``None`` for zero, blank or invalid dates."""
    m = _DATE_RE.match(text.strip("\x00 \t\r\n"))
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def parse_subsec_ms(text: str) -> int | None:
    """SubSecTime digits are a decimal fraction: ``"34"`` is 0.34 s -> 340 ms."""
    digits = text.strip("\x00 ")
    if not digits.isdigit() or not digits.isascii():
        return None
    return int(digits[:3].ljust(3, "0"))


def parse_offset(text: str) -> str | None:
    """``"+07:00"`` if ``text`` is a valid UTC offset, else ``None``."""
    m = _OFFSET_RE.match(text.strip("\x00 "))
    if not m or int(m.group(2)) > 14 or int(m.group(3)) > 59:
        return None
    return m.group(0)


def is_tiff_header(head: bytes) -> bool:
    if len(head) < 4 or head[:2] not in (b"II", b"MM"):
        return False
    bo = "<" if head[:2] == b"II" else ">"
    return int.from_bytes(head[2:4], "little" if bo == "<" else "big") in MAGICS


def parse_tiff(ra: RandomAccess, base: int = 0) -> ExifInfo | None:
    """Read capture tags from the TIFF structure whose header is at ``base``.

    Returns ``None`` when there is no valid TIFF header. A damaged IFD is skipped and
    whatever was read before it is kept. Offsets inside are relative to the header.
    """
    head = ra.read_upto(base, 8)
    if len(head) < 8 or not is_tiff_header(head):
        return None
    tiff = ra.window(base, ra.size - base)
    bo = "<" if head[:2] == b"II" else ">"
    tags: dict[int, str] = {}
    pending = [tiff.u32(4, bo)]
    seen: set[int] = set()
    while pending and len(seen) < MAX_IFDS:
        off = pending.pop(0)
        if off in seen or off < 8:
            continue
        seen.add(off)
        try:
            _walk_ifd(tiff, off, bo, tags, pending)
        except ParseError:
            continue
    return ExifInfo(
        dto=parse_exif_date(tags.get(TAG_DTO, "")),
        dtd=parse_exif_date(tags.get(TAG_DTD, "")),
        date_time=parse_exif_date(tags.get(TAG_DATETIME, "")),
        subsec_ms=parse_subsec_ms(tags.get(TAG_SUBSEC_ORIGINAL, "")),
        offset=parse_offset(tags.get(TAG_OFFSET_TIME_ORIGINAL, "")),
        make=_clean(tags.get(TAG_MAKE)),
        model=_clean(tags.get(TAG_MODEL)),
    )


def _walk_ifd(tiff: RandomAccess, off: int, bo: str, tags: dict[int, str],
              pending: list[int]) -> None:
    count = tiff.u16(off, bo)
    if count > MAX_ENTRIES:
        raise ParseError(f"IFD at {off} claims {count} entries")
    for i in range(count):
        entry = off + 2 + 12 * i
        tag = tiff.u16(entry, bo)
        if tag == TAG_EXIF_IFD:
            pending.append(tiff.u32(entry + 8, bo))
        elif tag in _ASCII_TAGS and tag not in tags:
            text = _read_text(tiff, entry, bo)
            if text is not None:
                tags[tag] = text


def _read_text(tiff: RandomAccess, entry: int, bo: str) -> str | None:
    typ = tiff.u16(entry + 2, bo)
    count = tiff.u32(entry + 4, bo)
    if typ not in _TEXT_TYPES or count == 0 or count > MAX_ASCII:
        return None
    try:
        raw = (tiff.read_at(entry + 8, count) if count <= 4
               else tiff.read_at(tiff.u32(entry + 8, bo), count))
    except ParseError:
        return None
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text or None
