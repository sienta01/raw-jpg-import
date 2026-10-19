"""Real JPEGs (via Pillow) and NEF-like raws that embed one, for the photo viewer tests."""

from __future__ import annotations

import io
import struct


def jpeg_bytes(w: int, h: int, color=(200, 40, 40), orientation: int | None = None) -> bytes:
    """A real JPEG of ``w`` x ``h`` with a blue marker top-left (needs Pillow)."""
    from PIL import Image

    im = Image.new("RGB", (w, h), color)
    im.paste((10, 10, 220), (0, 0, w // 4, h // 4))
    buf = io.BytesIO()
    kw = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kw["exif"] = exif.tobytes()
    im.save(buf, "JPEG", quality=90, **kw)
    return buf.getvalue()


def fake_nef(jpeg: bytes, *, orientation: int = 1, bo: str = "<", thumb: bytes = b"") -> bytes:
    """TIFF like a Nikon NEF: IFD0 (orientation, SubIFDs) -> a SubIFD holding ``jpeg``.

    ``thumb`` is a smaller JPEG referenced from IFD0 itself, as some raws do.
    """
    def entry(tag: int, typ: int, count: int, value: bytes) -> bytes:
        return struct.pack(bo + "HHI", tag, typ, count) + value

    def short(v: int) -> bytes:
        return struct.pack(bo + "HH", v, 0)

    def long_(v: int) -> bytes:
        return struct.pack(bo + "I", v)

    ifd0_off = 8
    ifd0_n = 2 + (2 if thumb else 0)
    sub_off = ifd0_off + 2 + ifd0_n * 12 + 4
    sub_n = 2
    data_off = sub_off + 2 + sub_n * 12 + 4
    thumb_off = data_off + len(jpeg)
    ifd0 = [entry(0x0112, 3, 1, short(orientation)), entry(0x014A, 4, 1, long_(sub_off))]
    if thumb:
        ifd0 += [entry(0x0201, 4, 1, long_(thumb_off)), entry(0x0202, 4, 1, long_(len(thumb)))]
    sub = [entry(0x0201, 4, 1, long_(data_off)), entry(0x0202, 4, 1, long_(len(jpeg)))]
    head = (b"II" if bo == "<" else b"MM") + struct.pack(bo + "HI", 42, ifd0_off)
    return (head + struct.pack(bo + "H", ifd0_n) + b"".join(ifd0) + long_(0)
            + struct.pack(bo + "H", sub_n) + b"".join(sub) + long_(0) + jpeg + thumb)
