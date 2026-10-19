"""Byte-exact ISO-BMFF builders: boxes, HEIF with an Exif item, and MOV/MP4 clips."""

from __future__ import annotations

import struct
from datetime import datetime, timedelta

from fakes.jpeg import seeded_bytes, size_for
from fakes.tiff import exif_date, make_tiff

EPOCH_1904 = datetime(1904, 1, 1)
APPLE_CREATION_DATE = b"com.apple.quicktime.creationdate"
APPLE_MAKE = b"com.apple.quicktime.make"


def u16(v: int) -> bytes:
    return struct.pack(">H", v)


def u32(v: int) -> bytes:
    return struct.pack(">I", v)


def u64(v: int) -> bytes:
    return struct.pack(">Q", v)


def box(typ: bytes, payload: bytes = b"", *, largesize: bool = False,
        size_zero: bool = False) -> bytes:
    """A box; ``largesize`` writes size 1 plus a u64, ``size_zero`` means "to end of file"."""
    if largesize:
        return u32(1) + typ + u64(len(payload) + 16) + payload
    if size_zero:
        return u32(0) + typ + payload
    return u32(len(payload) + 8) + typ + payload


def full_box(typ: bytes, version: int, flags: int, payload: bytes = b"") -> bytes:
    return box(typ, bytes([version]) + flags.to_bytes(3, "big") + payload)


def uuid_box(uuid: bytes, payload: bytes) -> bytes:
    return box(b"uuid", uuid + payload)


def ftyp(major: bytes, compatible: tuple[bytes, ...] = (), minor: int = 0) -> bytes:
    return box(b"ftyp", major + u32(minor) + b"".join(compatible))


def _uint(v: int, width: int) -> bytes:
    return v.to_bytes(width, "big") if width else b""


# --- HEIF -------------------------------------------------------------------------------

# iloc field sizes per version: (offset, length, base_offset, index). Chosen to cover
# every code path: base offsets in v0/v1, 64-bit offsets and extent indexes in v2.
_ILOC_SIZES = {0: (4, 4, 4, 0), 1: (4, 4, 4, 0), 2: (8, 4, 0, 4)}


def _infe(version: int, item_id: int, item_type: bytes) -> bytes:
    ident = u16(item_id) if version == 2 else u32(item_id)
    return full_box(b"infe", version, 0, ident + u16(0) + item_type + b"\x00")


def _iloc(version: int, items: list[tuple[int, int, int, int, int]]) -> bytes:
    """``items``: (item_id, construction, base_offset, extent_offset, extent_length)."""
    off_size, len_size, base_size, index_size = _ILOC_SIZES[version]
    body = bytes([off_size << 4 | len_size, base_size << 4 | index_size])
    body += u16(len(items)) if version < 2 else u32(len(items))
    for item_id, method, base, off, length in items:
        body += u16(item_id) if version < 2 else u32(item_id)
        if version in (1, 2):
            body += u16(method)
        body += u16(0) + _uint(base, base_size) + u16(1)
        body += _uint(0, index_size) + _uint(off, off_size) + _uint(length, len_size)
    return full_box(b"iloc", version, 0, body)


def heif_exif_blob(dt: datetime | str | None, *, exif_prefix: bool = True,
                   offset_field: bool = True, **tiff_kw) -> bytes:
    """Exif item payload: u32 BE offset to the TIFF header, optional ``Exif\\0\\0``, TIFF."""
    prefix = b"Exif\x00\x00" if exif_prefix else b""
    head = u32(len(prefix)) if offset_field else b""
    return head + prefix + make_tiff(dt, **tiff_kw)


def make_heif(dt: datetime | str | None, *, brand: bytes = b"heix", iloc_version: int = 1,
              construction: int = 0, exif_prefix: bool = True, offset_field: bool = True,
              with_exif: bool = True, infe_version: int | None = None,
              name: str = "DSC_0001.HIF", size: int | None = None, **tiff_kw) -> bytes:
    """A HEIF file: ftyp, meta (hdlr, pitm, iinf, iloc, idat?), mdat (image + Exif?).

    ``construction`` 0 stores the Exif item in ``mdat`` (file offset); 1 stores it in
    ``meta/idat``. iloc version 0 has no construction method, so it only allows 0.
    """
    if iloc_version == 0 and construction != 0:
        raise ValueError("iloc version 0 only supports construction method 0")
    if infe_version is None:
        infe_version = 3 if iloc_version == 2 else 2
    blob = heif_exif_blob(dt, exif_prefix=exif_prefix, offset_field=offset_field, **tiff_kw)
    head = ftyp(brand, (brand, b"mif1", b"heic"))

    def meta(image_at: int, image_len: int, exif_at: int) -> bytes:
        uses_base = _ILOC_SIZES[iloc_version][2] > 0
        infes = [_infe(infe_version, 1, b"hvc1")]
        locs = [(1, 0, image_at if uses_base else 0, 0 if uses_base else image_at, image_len)]
        if with_exif:
            infes.append(_infe(infe_version, 2, b"Exif"))
            if construction == 1:
                locs.append((2, 1, 0, 0, len(blob)))
            else:
                locs.append((2, 0, image_at if uses_base else 0,
                             exif_at - image_at if uses_base else exif_at, len(blob)))
        iinf_version = 0 if infe_version == 2 else 1
        count = u16(len(infes)) if iinf_version == 0 else u32(len(infes))
        kids = [
            full_box(b"hdlr", 0, 0, u32(0) + b"pict" + b"\x00" * 12 + b"\x00"),
            full_box(b"pitm", 0, 0, u16(1)),
            full_box(b"iinf", iinf_version, 0, count + b"".join(infes)),
            _iloc(iloc_version, locs),
        ]
        if with_exif and construction == 1:
            kids.append(box(b"idat", blob))
        return full_box(b"meta", 0, 0, b"".join(kids))

    in_mdat = blob if with_exif and construction == 0 else b""
    fixed = len(head) + len(meta(0, 0, 0)) + 8 + len(in_mdat)
    image = seeded_bytes(name, max(64, size_for(name, size) - fixed))
    image_at = len(head) + len(meta(0, 0, 0)) + 8
    exif_at = image_at + len(image)
    return head + meta(image_at, len(image), exif_at) + box(b"mdat", image + in_mdat)


# --- MOV / MP4 --------------------------------------------------------------------------


def video_mtime(start_local: datetime, duration_s: float) -> datetime:
    """The mtime a camera on FAT/exFAT leaves on a clip: its wall clock at the end."""
    return start_local + timedelta(seconds=duration_s)


def _mvhd(version: int, created: int, timescale: int, duration: int) -> bytes:
    if version == 1:
        times = u64(created) + u64(created) + u32(timescale) + u64(duration)
    else:
        times = u32(created) + u32(created) + u32(timescale) + u32(duration)
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    rest = u32(0x10000) + u16(0x100) + b"\x00" * 10 + matrix + b"\x00" * 24 + u32(2)
    return full_box(b"mvhd", version, 0, times + rest)


def nctg_record(tag: int, fmt: int, count: int, data: bytes, bo: str = ">") -> bytes:
    return struct.pack(bo + "IHH", tag, fmt, count) + data


def _nctg(start_local: datetime, tz_minutes: int, *, bo: str, tag: int,
          create: datetime | None, timezone: bool) -> bytes:
    def ascii_rec(t: int, text: str) -> bytes:
        raw = text.encode("ascii") + b"\x00"
        return nctg_record(t, 2, len(raw), raw, bo)

    recs = [ascii_rec(0x01, "NIKON"), ascii_rec(0x02, "NIKON Z f")]
    if create is not None:
        recs.append(ascii_rec(0x11, exif_date(create)))
    if tag in (0x11, 0x12):
        recs.append(ascii_rec(tag, exif_date(start_local)))
    else:   # a tag this parser does not know: only the regex fallback can find the date
        raw = exif_date(start_local).encode("ascii") + b"\x00"
        recs.append(nctg_record(tag, 7, len(raw), raw, bo))
    recs.append(nctg_record(0x13, 4, 1, struct.pack(bo + "I", 250), bo))
    recs.append(nctg_record(0x1200000, 1, 4, b"\x02\x03\x00\x00", bo))
    if timezone:
        recs.append(nctg_record(0x19, 8, 1, struct.pack(bo + "h", tz_minutes), bo))
    return b"".join(recs)


def iso_with_offset(start_local: datetime, tz_minutes: int, colon: bool = False) -> str:
    sign = "-" if tz_minutes < 0 else "+"
    h, m = divmod(abs(tz_minutes), 60)
    return start_local.strftime("%Y-%m-%dT%H:%M:%S") + f"{sign}{h:02d}{':' if colon else ''}{m:02d}"


def _data_box(text: str) -> bytes:
    return box(b"data", u32(1) + u32(0) + text.encode("utf-8"))


def _apple_meta(iso: str, *, fullbox: bool) -> bytes:
    keys = [APPLE_MAKE, APPLE_CREATION_DATE]
    entries = b"".join(u32(len(k) + 8) + b"mdta" + k for k in keys)
    kids = (full_box(b"hdlr", 0, 0, u32(0) + b"mdta" + b"\x00" * 12 + b"\x00")
            + full_box(b"keys", 0, 0, u32(len(keys)) + entries)
            + box(b"ilst", box(u32(1), _data_box("NIKON")) + box(u32(2), _data_box(iso))))
    return full_box(b"meta", 0, 0, kids) if fullbox else box(b"meta", kids)


def make_mov(start_local: datetime, tz_minutes: int = 0, duration_s: float = 10.0, *,
             nctg: bool = True, keys: bool = False, day: bool = False, mvhd_version: int = 0,
             moov_at_end: bool = True, largesize: bool = False, mdat_size_zero: bool = False,
             creation_time_zero: bool = False, nctg_bo: str = ">", nctg_tag: int = 0x12,
             nctg_create: datetime | None = None, nctg_timezone: bool = False,
             meta_fullbox: bool = False, brand: bytes = b"qt  ",
             name: str = "DSC_0001.MOV", size: int | None = None) -> bytes:
    """A MOV clip shot at camera wall-clock ``start_local`` in zone UTC+``tz_minutes``.

    mvhd holds UTC (as Nikon writes it); NCTG holds local time; Apple keys and (c)day hold
    ISO-8601 with the offset. Write the file with mtime ``video_mtime(start, duration)``.
    """
    if mdat_size_zero and moov_at_end:
        raise ValueError("a size-0 mdat must be the last box")
    utc = start_local - timedelta(minutes=tz_minutes)
    created = 0 if creation_time_zero else int((utc - EPOCH_1904).total_seconds())
    timescale = 90000 if mvhd_version == 1 else 1000
    kids = [_mvhd(mvhd_version, created, timescale, round(duration_s * timescale)),
            box(b"trak", full_box(b"tkhd", 0, 3, b"\x00" * 80))]
    udta = b""
    if nctg:
        nchd = box(b"NCHD", u32(1) + b"NIKON\x00")
        udta += box(b"NCDT", nchd + box(b"NCTG", _nctg(
            start_local, tz_minutes, bo=nctg_bo, tag=nctg_tag, create=nctg_create,
            timezone=nctg_timezone)))
    if day:
        text = iso_with_offset(start_local, tz_minutes).encode("ascii")
        udta += box(b"\xa9day", u16(len(text)) + u16(0x55C4) + text)
    if udta:
        kids.append(box(b"udta", udta))
    if keys:
        kids.append(_apple_meta(iso_with_offset(start_local, tz_minutes, colon=False),
                                fullbox=meta_fullbox))
    moov = box(b"moov", b"".join(kids))
    head = ftyp(brand, (brand,), 0x20050300 if brand == b"qt  " else 0) + box(b"wide")
    mdat_header = 16 if largesize else 8
    filler = max(64, size_for(name, size) - len(head) - len(moov) - mdat_header)
    mdat = box(b"mdat", seeded_bytes(name, filler), largesize=largesize,
               size_zero=mdat_size_zero)
    return head + (mdat + moov if moov_at_end else moov + mdat)


def make_mp4(start_local: datetime, tz_minutes: int = 0, duration_s: float = 10.0,
             **kw) -> bytes:
    """An MP4 clip: like :func:`make_mov` but ISO brand and, by default, no Nikon NCTG."""
    kw.setdefault("nctg", False)
    kw.setdefault("brand", b"mp42")
    kw.setdefault("name", "C0001.MP4")
    kw.setdefault("meta_fullbox", True)
    return make_mov(start_local, tz_minutes, duration_s, **kw)
