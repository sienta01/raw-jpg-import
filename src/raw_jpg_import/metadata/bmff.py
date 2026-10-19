"""ISO-BMFF box iteration, the HEIF Exif item and the Canon CR3 metadata boxes."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from ._io import Cursor, ParseError, RandomAccess
from .tiff import ExifInfo, is_tiff_header, parse_tiff

log = logging.getLogger(__name__)

MAX_BOXES = 4096            # per level; every box is at least 8 bytes, so this only bounds junk
MAX_SMALL_BOX = 4 * 1024 * 1024     # iinf / iloc / keys payloads are read into memory
MAX_ITEMS = 65536
MAX_EXTENTS = 1024
MAX_EXIF_ITEM = 4 * 1024 * 1024

CR3_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
TOP_LEVEL_TYPES = frozenset({b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot",
                             b"meta", b"uuid"})
HEIF_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm",
                         b"hevs", b"mif1", b"mif2", b"msf1", b"avif", b"avis"})


@dataclass(frozen=True)
class Box:
    type: bytes            # four-character code
    start: int             # offset of the box header
    size: int              # whole box, header included
    header: int            # 8, 16 with a largesize, +16 for a uuid box
    uuid: bytes | None = None

    @property
    def payload(self) -> int:
        return self.start + self.header

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def payload_size(self) -> int:
        return self.size - self.header


def is_bmff(head: bytes) -> bool:
    return len(head) >= 8 and head[4:8] in TOP_LEVEL_TYPES


def iter_boxes(ra: RandomAccess, start: int = 0, end: int | None = None) -> Iterator[Box]:
    """Boxes between ``start`` and ``end``. Stops quietly at the first malformed header.

    Size 1 means a 64-bit size follows; size 0 means "to the end of the parent".
    Payloads are never read here, so a huge ``mdat`` costs one 16-byte read.
    """
    end = ra.size if end is None else min(end, ra.size)
    pos = start
    for _ in range(MAX_BOXES):
        if pos + 8 > end:
            return
        size = ra.u32(pos)
        typ = ra.read_at(pos + 4, 4)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = ra.uint(pos + 8, 8)
            header = 16
        elif size == 0:
            size = end - pos
        uuid = None
        if typ == b"uuid":
            if pos + header + 16 > end:
                return
            uuid = ra.read_at(pos + header, 16)
            header += 16
        if size < header or pos + size > end:
            return
        yield Box(typ, pos, size, header, uuid)
        pos += size


def find_box(ra: RandomAccess, typ: bytes, start: int = 0, end: int | None = None) -> Box | None:
    for box in iter_boxes(ra, start, end):
        if box.type == typ:
            return box
    return None


def major_brand(ra: RandomAccess) -> bytes | None:
    head = ra.read_upto(0, 12)
    return head[8:12] if len(head) == 12 and head[4:8] == b"ftyp" else None


def is_fourcc(b: bytes) -> bool:
    return len(b) == 4 and all(0x20 <= c <= 0x7E or c == 0xA9 for c in b)


def meta_children(ra: RandomAccess, meta: Box) -> int:
    """Where the children of a ``meta`` box start.

    ISO files make ``meta`` a FullBox (4 bytes of version/flags first); QuickTime files
    do not. A child box type at offset 4 means there is no version/flags field.
    """
    head = ra.read_upto(meta.payload, 8)
    if len(head) == 8 and is_fourcc(head[4:8]) and int.from_bytes(head[:4], "big") >= 8:
        return meta.payload
    return meta.payload + 4


def read_payload(ra: RandomAccess, box: Box, limit: int = MAX_SMALL_BOX) -> bytes:
    if box.payload_size > limit:
        raise ParseError(f"{box.type!r} box too large ({box.payload_size} bytes)")
    return ra.read_at(box.payload, box.payload_size)


# --- HEIF -------------------------------------------------------------------------------


def heif_exif(ra: RandomAccess) -> ExifInfo | None:
    """Capture tags from the ``Exif`` item of a HEIF/HEIC/AVIF file. Never checks the brand."""
    log.debug("HEIF brand %r", major_brand(ra))
    meta = find_box(ra, b"meta")
    if meta is None:
        return None
    kids: dict[bytes, Box] = {}
    for b in iter_boxes(ra, meta_children(ra, meta), meta.end):
        kids.setdefault(b.type, b)
    if b"iinf" not in kids or b"iloc" not in kids:
        return None
    exif_ids = _exif_item_ids(ra, kids[b"iinf"])
    if not exif_ids:
        return None
    for item_id, method, base, extents in _iloc_items(read_payload(ra, kids[b"iloc"])):
        if item_id not in exif_ids:
            continue
        blob = _item_data(ra, method, base, extents, kids.get(b"idat"))
        if blob is not None:
            info = _parse_exif_blob(blob)
            if info is not None:
                return info
    return None


def _exif_item_ids(ra: RandomAccess, iinf: Box) -> set[int]:
    version = ra.read_at(iinf.payload, 1)[0]
    first = iinf.payload + 4 + (2 if version == 0 else 4)
    ids: set[int] = set()
    for infe in iter_boxes(ra, first, iinf.end):
        if infe.type != b"infe" or infe.payload_size < 8:
            continue
        cur = Cursor(ra.read_at(infe.payload, min(infe.payload_size, 16)))
        v = cur.u8()
        cur.skip(3)
        if v < 2:
            continue                # v0/v1 entries carry no item type
        item_id = cur.u16() if v == 2 else cur.u32()
        cur.skip(2)                 # item_protection_index
        if cur.take(4) == b"Exif":
            ids.add(item_id)
    return ids


def _iloc_items(data: bytes) -> Iterator[tuple[int, int, int, list[tuple[int, int]]]]:
    """``(item_id, construction_method, base_offset, [(offset, length), ...])`` per item."""
    cur = Cursor(data)
    version = cur.u8()
    cur.skip(3)
    if version > 2:
        raise ParseError(f"iloc version {version}")
    sizes = cur.u8()
    offset_size, length_size = sizes >> 4, sizes & 0xF
    sizes = cur.u8()
    base_size = sizes >> 4
    index_size = sizes & 0xF if version in (1, 2) else 0
    for s in (offset_size, length_size, base_size, index_size):
        if s not in (0, 4, 8):
            raise ParseError(f"iloc field size {s}")
    count = cur.u16() if version < 2 else cur.u32()
    for _ in range(min(count, MAX_ITEMS)):
        item_id = cur.u16() if version < 2 else cur.u32()
        method = cur.u16() & 0xF if version in (1, 2) else 0
        cur.skip(2)                 # data_reference_index
        base = cur.uint(base_size)
        n = cur.u16()
        if n > MAX_EXTENTS:
            raise ParseError(f"iloc item with {n} extents")
        extents = []
        for _ in range(n):
            cur.skip(index_size)
            extents.append((cur.uint(offset_size), cur.uint(length_size)))
        yield item_id, method, base, extents


def _item_data(ra: RandomAccess, method: int, base: int, extents: list[tuple[int, int]],
               idat: Box | None) -> bytes | None:
    if method == 0:
        src = ra
    elif method == 1 and idat is not None:
        src = ra.window(idat.payload, idat.payload_size)
    else:
        return None                 # construction 2 (item references) is not used for Exif
    parts: list[bytes] = []
    total = 0
    for off, length in extents:
        start = base + off
        if length == 0:             # "to the end of the data"
            length = src.size - start
        total += length
        if total > MAX_EXIF_ITEM:
            return None
        parts.append(src.read_at(start, length))
    return b"".join(parts) if parts else None


def _parse_exif_blob(blob: bytes) -> ExifInfo | None:
    """Exif item payload: u32 BE offset to the TIFF header (after itself), then the data."""
    ra = RandomAccess.from_bytes(blob)
    if len(blob) >= 4:
        start = 4 + int.from_bytes(blob[:4], "big")
        if start + 8 <= len(blob) and is_tiff_header(blob[start:start + 4]):
            return parse_tiff(ra, start)
    for marker in (b"II*\x00", b"MM\x00*"):
        at = blob.find(marker, 0, 64 + 4)
        if at >= 0:
            return parse_tiff(ra, at)
    return None


# --- Canon CR3 --------------------------------------------------------------------------


def cr3_exif(ra: RandomAccess) -> ExifInfo | None:
    """Capture tags from CMT1 (IFD0) and CMT2 (Exif IFD) inside ``moov/uuid``."""
    moov = find_box(ra, b"moov")
    if moov is None:
        return None
    canon = next((b for b in iter_boxes(ra, moov.payload, moov.end)
                  if b.type == b"uuid" and b.uuid == CR3_UUID), None)
    if canon is None:
        return None
    found: dict[bytes, ExifInfo] = {}
    for box in iter_boxes(ra, canon.payload, canon.end):
        if box.type in (b"CMT1", b"CMT2") and box.type not in found:
            try:
                info = parse_tiff(ra.window(box.payload, box.payload_size))
            except ParseError:
                continue
            if info is not None:
                found[box.type] = info
    if not found:
        return None
    exif = found.get(b"CMT2")
    ifd0 = found.get(b"CMT1")
    return exif.merged(ifd0) if exif is not None else ifd0
