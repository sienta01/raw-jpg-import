"""MOV/MP4 capture time: Nikon NCTG, Apple keys / (c)day, then mvhd with an inferred offset.

Priority (first hit wins):

1. ``NCDT/NCTG`` (Nikon maker tags): DateTimeOriginal (0x12), else CreateDate (0x11),
   recorded as the camera's LOCAL wall-clock time. Exact.
2. ``meta/keys`` + ``ilst`` ``com.apple.quicktime.creationdate``, else ``udta/(c)day``:
   ISO-8601 with an offset; the wall-clock part is kept. Exact.
3. ``mvhd`` creation time (seconds since 1904, usually UTC). The camera's offset is
   inferred from the card mtime, which FAT/exFAT store as camera wall-clock time at the
   moment the file was closed (the end of the clip). Not exact.
4. mtime minus the clip duration. Not exact.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from ..models import Capture
from ._io import Cursor, ParseError, RandomAccess
from .bmff import Box, find_box, iter_boxes, meta_children, read_payload
from .tiff import parse_exif_date

T = TypeVar("T")

MAX_DEPTH = 5
MAX_DURATION = timedelta(days=7)
MAX_MP4_SECONDS = 400 * 365 * 86400         # anything later than ~2300 is garbage
EPOCH_1904 = datetime(1904, 1, 1)
QUARTER_HOUR = timedelta(minutes=15)
OFFSET_TOLERANCE = timedelta(minutes=3)
MAX_OFFSET = timedelta(hours=14)

NCTG_CREATE_DATE = 0x11
NCTG_DATE_TIME_ORIGINAL = 0x12
NCTG_TIME_ZONE = 0x19
# EXIF format code -> bytes per value
_EXIF_FORMAT_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
                      11: 4, 12: 8, 13: 4}

APPLE_CREATION_DATE = b"com.apple.quicktime.creationdate"
_CONTAINERS = frozenset({b"moov", b"udta", b"meta", b"trak", b"NCDT", b"ilst"})
_DAY = b"\xa9day"

_EXIF_DATE_RE = re.compile(rb"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d")
_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?"
)


@dataclass
class _Found:
    mvhd: Box | None = None
    nctg: list[Box] = field(default_factory=list)
    metas: list[Box] = field(default_factory=list)
    days: list[Box] = field(default_factory=list)


@dataclass(frozen=True)
class _Mvhd:
    creation: datetime | None       # naive, as stored (UTC for most cameras)
    duration: timedelta | None


def read_video(ra: RandomAccess, mtime_local: datetime | None) -> Capture | None:
    """Capture time of a MOV/MP4, or ``None`` when the file has no usable ``moov``.

    ``mtime_local`` is the file's mtime as naive local time (``None`` if unknown).
    Results follow the module docstring's priority order.
    """
    moov = find_box(ra, b"moov")
    if moov is None:
        return None
    found = _Found()
    _scan(ra, moov, 0, found)

    # A damaged source must not hide the next one, so each step fails on its own.
    for box in found.nctg:
        cap = _guard(lambda b=box: _nctg_capture(read_payload(ra, b)))
        if cap is not None:
            return cap
    cap = _keys_capture(ra, found)
    if cap is not None:
        return cap
    mvhd = _guard(lambda: _read_mvhd(ra, found.mvhd)) if found.mvhd is not None else None
    if mvhd is not None and mvhd.creation is not None:
        return _mvhd_capture(mvhd.creation, mvhd.duration, mtime_local)
    if mvhd is not None and mvhd.duration is not None and mtime_local is not None:
        return Capture(local=_round_seconds(mtime_local - mvhd.duration), source="mtime",
                       exact=False)
    return None


def _guard(step: Callable[[], T]) -> T | None:
    try:
        return step()
    except ParseError:
        return None


def _scan(ra: RandomAccess, parent: Box, depth: int, found: _Found) -> None:
    start = meta_children(ra, parent) if parent.type == b"meta" else parent.payload
    for box in iter_boxes(ra, start, parent.end):
        if box.type == b"mvhd" and found.mvhd is None and depth == 0:
            found.mvhd = box
        elif box.type == b"NCTG":
            found.nctg.append(box)
        elif box.type == _DAY:
            found.days.append(box)
        elif box.type in _CONTAINERS and depth < MAX_DEPTH:
            if box.type == b"meta":
                found.metas.append(box)
            _scan(ra, box, depth + 1, found)


# --- 1. Nikon NCTG ----------------------------------------------------------------------


def _nctg_capture(payload: bytes) -> Capture | None:
    """Nikon tag records: u32 tag, u16 EXIF format, u16 count, data (big-endian)."""
    for bo in (">", "<"):          # byte order not yet confirmed on a real Zf file
        records = _nctg_records(payload, bo)
        for tag in (NCTG_DATE_TIME_ORIGINAL, NCTG_CREATE_DATE):
            fmt_value = records.get(tag)
            if fmt_value and fmt_value[0] in (2, 7):
                local = parse_exif_date(fmt_value[1].decode("ascii", "replace"))
                if local is not None:
                    return Capture(local=local, source="nctg", exact=True,
                                   offset=_nctg_offset(records.get(NCTG_TIME_ZONE), bo))
    m = _EXIF_DATE_RE.search(payload)
    local = parse_exif_date(m.group().decode("ascii")) if m else None
    return Capture(local=local, source="nctg", exact=True) if local else None


def _nctg_records(payload: bytes, bo: str) -> dict[int, tuple[int, bytes]]:
    """``{tag: (format, data)}``; stops at the first unknown format or truncated record."""
    cur = Cursor(payload, bo=bo)
    records: dict[int, tuple[int, bytes]] = {}
    while cur.remaining >= 8:
        tag, fmt, count = cur.u32(), cur.u16(), cur.u16()
        size = count * _EXIF_FORMAT_SIZES.get(fmt, 0)
        if fmt not in _EXIF_FORMAT_SIZES or size > cur.remaining:
            break
        records.setdefault(tag, (fmt, cur.take(size)))
    return records


def _nctg_offset(record: tuple[int, bytes] | None, bo: str) -> str | None:
    """TimeZone (0x19): accepted as int16 minutes or a ``+hh:mm`` string, else ignored."""
    if record is None:
        return None
    fmt, data = record
    if fmt == 8 and len(data) >= 2:
        return _format_offset(timedelta(minutes=struct.unpack(bo + "h", data[:2])[0]))
    if fmt == 2:
        m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", data.split(b"\0")[0].decode("ascii", "ignore"))
        if m:
            return m.group(0)
    return None


# --- 2. Apple keys / (c)day -------------------------------------------------------------


def _keys_capture(ra: RandomAccess, found: _Found) -> Capture | None:
    texts = [lambda m=meta: _apple_creation_date(ra, m) for meta in found.metas]
    texts += [lambda b=box: _day_text(read_payload(ra, b, 4096)) for box in found.days]
    for read_text in texts:
        text = _guard(read_text)
        cap = _iso_capture(text) if text else None
        if cap is not None:
            return cap
    return None


def _apple_creation_date(ra: RandomAccess, meta: Box) -> str | None:
    kids: dict[bytes, Box] = {}
    for b in iter_boxes(ra, meta_children(ra, meta), meta.end):
        kids.setdefault(b.type, b)
    if b"keys" not in kids or b"ilst" not in kids:
        return None
    cur = Cursor(read_payload(ra, kids[b"keys"], 1024 * 1024))
    cur.skip(4)
    index = None
    for i in range(1, min(cur.u32(), 4096) + 1):
        size = cur.u32()
        if size < 8:
            raise ParseError("bad keys entry")
        cur.skip(4)                 # namespace, usually "mdta"
        if cur.take(size - 8) == APPLE_CREATION_DATE:
            index = i
            break
    if index is None:
        return None
    ilst = kids[b"ilst"]
    for item in iter_boxes(ra, ilst.payload, ilst.end):
        if item.type == struct.pack(">I", index):
            return _data_box_text(ra, item)
    return None


def _data_box_text(ra: RandomAccess, item: Box) -> str | None:
    data = find_box(ra, b"data", item.payload, item.end)
    if data is None or data.payload_size < 8:
        return None
    raw = read_payload(ra, data, 4096)[8:]      # skip type indicator and locale
    return raw.split(b"\0")[0].decode("utf-8", "replace")


def _day_text(payload: bytes) -> str | None:
    """A ``(c)day`` box holds either an iTunes ``data`` box or QuickTime user-data text."""
    if len(payload) >= 16 and payload[4:8] == b"data":
        return payload[16:].split(b"\0")[0].decode("utf-8", "replace")
    if len(payload) >= 4:
        n = int.from_bytes(payload[:2], "big")
        return payload[4:4 + n].split(b"\0")[0].decode("utf-8", "replace")
    return None


def _iso_capture(text: str) -> Capture | None:
    m = _ISO_RE.match(text)
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        local = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    except ValueError:
        return None
    subsec = int(frac[:3].ljust(3, "0")) if frac else None
    return Capture(local=local, source="qt-keys", exact=True, subsec_ms=subsec,
                   offset=_iso_offset(tz))


def _iso_offset(tz: str | None) -> str | None:
    if not tz:
        return None
    if tz == "Z":
        return "+00:00"
    digits = tz[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
    if hours > 14 or minutes > 59:
        return None
    return f"{tz[0]}{hours:02d}:{minutes:02d}"


# --- 3./4. mvhd and mtime ---------------------------------------------------------------


def _read_mvhd(ra: RandomAccess, box: Box) -> _Mvhd:
    cur = Cursor(read_payload(ra, box, 1024))
    version = cur.u8()
    cur.skip(3)
    if version == 1:
        creation, _modified, timescale, duration = cur.u64(), cur.u64(), cur.u32(), cur.u64()
    else:
        creation, _modified, timescale, duration = cur.u32(), cur.u32(), cur.u32(), cur.u32()
    created = None
    if 0 < creation <= MAX_MP4_SECONDS:
        created = EPOCH_1904 + timedelta(seconds=creation)
    length = None
    if timescale:
        seconds = duration / timescale
        if seconds <= MAX_DURATION.total_seconds():
            length = timedelta(seconds=seconds)
    return _Mvhd(created, length)


def _mvhd_capture(stored: datetime, duration: timedelta | None,
                  mtime_local: datetime | None) -> Capture:
    """``stored`` is mvhd's naive time; the shift to camera time is guessed from mtime."""
    if mtime_local is not None:
        start_guess = mtime_local - (duration or timedelta(0))
        off = start_guess - stored
        q = round(off / QUARTER_HOUR) * QUARTER_HOUR
        if abs(off - q) <= OFFSET_TOLERANCE and abs(q) <= MAX_OFFSET:
            return Capture(local=stored + q, source="mvhd+mtime", exact=False)
    return Capture(local=_utc_to_pc_local(stored), source="mvhd-pc-tz", exact=False)


def _utc_to_pc_local(stored: datetime) -> datetime:
    try:
        return stored.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return stored


def _format_offset(off: timedelta) -> str | None:
    minutes = int(off.total_seconds() // 60)
    if abs(minutes) > 14 * 60:
        return None
    sign = "-" if minutes < 0 else "+"
    h, m = divmod(abs(minutes), 60)
    return f"{sign}{h:02d}:{m:02d}"


def _round_seconds(dt: datetime) -> datetime:
    return (dt + timedelta(microseconds=500_000)).replace(microsecond=0)
