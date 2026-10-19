"""Fake raw files: TIFF-based raws, Fujifilm RAF and Canon CR3, with name-seeded padding.

Contents and (by default) sizes depend on ``name``, so two fakes are never identical
unless they are meant to be (same name, same date, same size).
"""

from __future__ import annotations

import struct
from datetime import datetime

from fakes.bmff import box, ftyp, uuid_box
from fakes.jpeg import make_jpeg, seeded_bytes, size_for
from fakes.tiff import make_tiff

CR3_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
RAF_MAGIC = b"FUJIFILMCCD-RAW "
RAF_HEADER_SIZE = 160


def _tiff_raw(dto: datetime | str | None, name: str, size: int | None, **kw) -> bytes:
    tiff = make_tiff(dto, **kw)
    return tiff + seeded_bytes(name, max(0, size_for(name, size) - len(tiff)))


def make_nef(dto: datetime | str | None, *, name: str = "DSC_0001.NEF",
             size: int | None = None, **kw) -> bytes:
    kw.setdefault("make", "NIKON CORPORATION")
    kw.setdefault("model", "NIKON Z f")
    return _tiff_raw(dto, name, size, **kw)


def make_arw(dto: datetime | str | None, *, name: str = "DSC00001.ARW",
             size: int | None = None, **kw) -> bytes:
    kw.setdefault("make", "SONY")
    kw.setdefault("model", "ILCE-7M4")
    return _tiff_raw(dto, name, size, **kw)


def make_dng(dto: datetime | str | None, *, name: str = "L1000001.DNG",
             size: int | None = None, **kw) -> bytes:
    kw.setdefault("make", "LEICA CAMERA AG")
    kw.setdefault("model", "LEICA Q3")
    return _tiff_raw(dto, name, size, **kw)


def make_orf(dto: datetime | str | None, *, name: str = "P1010001.ORF",
             size: int | None = None, **kw) -> bytes:
    kw.setdefault("magic", 0x4F52)
    kw.setdefault("make", "OM Digital Solutions")
    kw.setdefault("model", "OM-1")
    return _tiff_raw(dto, name, size, **kw)


def make_rw2(dto: datetime | str | None, *, name: str = "P1000001.RW2",
             size: int | None = None, **kw) -> bytes:
    kw.setdefault("magic", 0x55)
    kw.setdefault("make", "Panasonic")
    kw.setdefault("model", "DC-S5M2")
    return _tiff_raw(dto, name, size, **kw)


def make_raf(dto: datetime | str | None, *, name: str = "DSCF0001.RAF",
             size: int | None = None, **jpeg_kw) -> bytes:
    """RAF header (JPEG offset/length as u32 BE at bytes 84/88), embedded JPEG, padding."""
    jpeg_kw.setdefault("make", "FUJIFILM")
    jpeg_kw.setdefault("model", "X-T5")
    preview = make_jpeg(dto, name=name + ".preview", size=8 * 1024, **jpeg_kw)
    header = (RAF_MAGIC + b"0201" + b"FF129502" + b"X-T5".ljust(32, b"\x00") + b"0100"
              + b"\x00" * 20 + struct.pack(">II", RAF_HEADER_SIZE, len(preview)))
    header = header.ljust(RAF_HEADER_SIZE, b"\x00")
    body = header + preview
    return body + seeded_bytes(name, max(0, size_for(name, size) - len(body)))


def make_cr3(dto: datetime | str | None, *, name: str = "IMG_0001.CR3",
             size: int | None = None, ifd0_datetime: datetime | str | None = None,
             bo: str = "<") -> bytes:
    """``ftyp crx`` + ``moov{uuid{CNCV, CMT1 (IFD0), CMT2 (Exif IFD)}}`` + ``mdat``."""
    cmt1 = make_tiff(None, bo=bo, make="Canon", model="Canon EOS R6m2",
                     ifd0_datetime=dto if ifd0_datetime is None else ifd0_datetime)
    cmt2 = make_tiff(dto, bo=bo, make=None, model=None, exif_in_ifd0=True, subsec="00")
    canon = uuid_box(CR3_UUID, box(b"CNCV", b"CanonCR3_001/00.09.00/00.00.00")
                     + box(b"CMT1", cmt1) + box(b"CMT2", cmt2))
    head = ftyp(b"crx ", (b"crx ", b"isom"), 1)
    moov = box(b"moov", canon)
    filler = max(64, size_for(name, size) - len(head) - len(moov) - 8)
    return head + moov + box(b"mdat", seeded_bytes(name, filler))
