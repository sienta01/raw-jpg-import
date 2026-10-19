"""Previews of imported photos for the built-in viewer.

Listing an event folder and finding the JPEG a camera embeds in its raw files are stdlib
only. Decoding needs Pillow, which is optional for a source install (``pip install pillow``)
and bundled with the standalone apps; without it :func:`load_preview` returns ``None`` and
the viewer still lists the files. Nothing here writes anywhere, and everything is safe to
call from worker threads (no Tk).
"""

from __future__ import annotations

import io
import os
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .formats import FormatTable, split_ext
from .fsops import is_online_only
from .naming import key

JPEG_EXTS = frozenset({"jpg", "jpeg", "jpe"})
HEIF_EXTS = frozenset({"hif", "heif", "heic"})
PLAIN_IMAGE_EXTS = frozenset({"png", "tif", "tiff", "bmp", "gif", "webp"})

MAX_EMBEDDED = 64 * 1024 * 1024        # largest embedded JPEG we are willing to read
_MAX_IFDS = 24
_MAX_ENTRIES = 1024

# EXIF orientation -> Pillow transpose method name (1 = upright, nothing to do).
_ORIENT = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
           6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}

_pil: Any = None
_pil_checked = False
_heif_ready = False


def pillow() -> Any | None:
    """The ``PIL.Image`` module, or None when Pillow is not installed."""
    global _pil, _pil_checked, _heif_ready
    if not _pil_checked:
        _pil_checked = True
        try:
            from PIL import Image

            _pil = Image
        except ImportError:
            _pil = None
        if _pil is not None:
            try:                                    # optional: HEIF (.HIF) decoding
                from pillow_heif import register_heif_opener  # type: ignore[import-not-found]

                register_heif_opener()
                _heif_ready = True
            except Exception:
                _heif_ready = False
    return _pil


def can_decode_heif() -> bool:
    pillow()
    return _heif_ready


# ---- the event folder ------------------------------------------------------------------------


@dataclass(frozen=True)
class LibraryFile:
    path: Path
    folder: str              # subfolder of the event folder ("RAW", "JPG", "MOV"; "" at top)
    kind: str                # "raw" | "jpg" | "heif" | "video" | "sidecar" | "other"
    size: int
    mtime_ns: int
    online_only: bool = False   # a cloud placeholder: reading it would download the file

    @property
    def label(self) -> str:
        return split_ext(self.path.name)[1].upper() or self.path.name


@dataclass
class LibraryShot:
    """All files of one shot in an event folder: DSC_0001.NEF, DSC_0001.JPG, ..."""
    key: str
    name: str
    files: list[LibraryFile] = field(default_factory=list)

    @property
    def kinds(self) -> list[str]:
        order = ["raw", "jpg", "heif", "video", "other", "sidecar"]
        present = {f.kind for f in self.files}
        return [k for k in order if k in present]

    @property
    def in_cloud(self) -> bool:
        """Every file is a cloud placeholder, so a preview would download it."""
        return all(f.online_only for f in self.files)

    @property
    def taken_ns(self) -> int:
        """Card timestamps are kept on import, so the earliest mtime is the shooting time."""
        return min(f.mtime_ns for f in self.files)

    def preview_order(self) -> list[LibraryFile]:
        """Files to try for a preview, cheapest and most faithful first."""
        rank = {"jpg": 0, "heif": 1 if can_decode_heif() else 3, "raw": 2, "other": 4,
                "video": 5, "sidecar": 6}
        return sorted(self.files, key=lambda f: (rank.get(f.kind, 9),
                                                 _shot_key(f.path.name) != f.path.name.split(
                                                     ".", 1)[0],       # -v2 after the main
                                                 f.path.name))


def _kind(name: str, fmt: FormatTable | None) -> str:
    _, ext = split_ext(name)
    if ext in JPEG_EXTS:
        return "jpg"
    if ext in HEIF_EXTS:
        return "heif"
    cat = fmt.classify(name) if fmt is not None else None
    if cat is not None:
        if cat.role == "sidecar":
            return "sidecar"
        if cat.key in ("raw", "video"):
            return cat.key
        if cat.key in ("jpg", "heif"):
            return cat.key
    return "other"


def _shot_key(name: str) -> str:
    stem = name.split(".", 1)[0]
    if "-v" in stem:
        head, _, tail = stem.rpartition("-v")
        if tail.isdigit() and head:
            stem = head
    return stem


def _iter_files(folder: Path) -> Iterable[tuple[Path, str, os.stat_result]]:
    """Regular files in ``folder`` and its direct subfolders; hidden and temp files skipped."""
    try:
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name.casefold())
    except OSError:
        return
    for e in entries:
        if e.name.startswith(".") or e.name.casefold().endswith(".part"):
            continue
        try:
            if e.is_file(follow_symlinks=False):
                yield Path(e.path), "", e.stat(follow_symlinks=False)
            elif e.is_dir(follow_symlinks=False):
                with os.scandir(e.path) as sub:
                    for s in sorted(sub, key=lambda x: x.name.casefold()):
                        if s.name.startswith(".") or s.name.casefold().endswith(".part"):
                            continue
                        if s.is_file(follow_symlinks=False):
                            yield Path(s.path), e.name, s.stat(follow_symlinks=False)
        except OSError:
            continue


def list_library(event_folder: Path, fmt: FormatTable | None = None) -> list[LibraryShot]:
    """The shots in an event folder, in shooting order (by the preserved card times)."""
    shots: dict[str, LibraryShot] = {}
    for path, folder, st in _iter_files(event_folder):
        stem = _shot_key(path.name)
        k = key(stem)
        shot = shots.get(k)
        if shot is None:
            shot = shots[k] = LibraryShot(key=k, name=stem)
        shot.files.append(LibraryFile(path, folder, _kind(path.name, fmt), st.st_size,
                                      st.st_mtime_ns, is_online_only(st)))
    out = list(shots.values())
    for s in out:
        s.files.sort(key=lambda f: (f.kind != "raw", f.path.name.casefold()))
    out.sort(key=lambda s: (s.taken_ns, s.key))
    return out


# ---- embedded JPEG previews in raw files -----------------------------------------------------


def _read(f: BinaryIO, off: int, n: int) -> bytes:
    f.seek(off)
    data = f.read(n)
    if len(data) != n:
        raise EOFError
    return data


def _tiff_previews(f: BinaryIO, size: int) -> tuple[list[tuple[int, int]], int]:
    """``([(offset, length) of embedded JPEGs], orientation)`` of a TIFF-based raw file."""
    head = _read(f, 0, 8)
    bo = {b"II": "<", b"MM": ">"}.get(head[:2])
    if bo is None:
        return [], 1
    magic = struct.unpack(bo + "H", head[2:4])[0]
    if magic not in (42, 0x4F52, 0x5352, 0x55):
        return [], 1
    first = struct.unpack(bo + "I", head[4:8])[0]
    todo: list[tuple[int, bool]] = [(first, True)]
    seen: set[int] = set()
    found: list[tuple[int, int]] = []
    orientation = 1
    while todo and len(seen) < _MAX_IFDS:
        off, is_ifd0 = todo.pop(0)
        if off in seen or not 8 <= off < size - 2:
            continue
        seen.add(off)
        count = struct.unpack(bo + "H", _read(f, off, 2))[0]
        if count > _MAX_ENTRIES:
            continue
        raw = _read(f, off + 2, count * 12 + 4)
        tags: dict[int, list[int]] = {}
        for i in range(count):
            tag, typ, n = struct.unpack(bo + "HHI", raw[i * 12:i * 12 + 8])
            width = {3: 2, 4: 4, 13: 4}.get(typ)
            if width is None or n == 0 or n > 64:
                continue
            fmt_char = "H" if width == 2 else "I"
            value_bytes = raw[i * 12 + 8:i * 12 + 12]
            if width * n > 4:
                value_off = struct.unpack(bo + "I", value_bytes)[0]
                if value_off + width * n > size:
                    continue
                value_bytes = _read(f, value_off, width * n)
            tags[tag] = list(struct.unpack(bo + fmt_char * n, value_bytes[:width * n]))
        if is_ifd0 and 0x0112 in tags:
            orientation = tags[0x0112][0]
        if 0x0201 in tags and 0x0202 in tags:
            found.append((tags[0x0201][0], tags[0x0202][0]))
        compression = tags.get(0x0103, [0])[0]
        strips, counts = tags.get(0x0111, []), tags.get(0x0117, [])
        if compression in (6, 7) and len(strips) == 1 and len(counts) == 1:
            found.append((strips[0], counts[0]))      # a JPEG stored as one strip (DNG)
        for sub in tags.get(0x014A, []):              # SubIFDs: Nikon keeps JpgFromRaw here
            todo.append((sub, False))
        next_ifd = struct.unpack(bo + "I", raw[count * 12:count * 12 + 4])[0]
        if next_ifd:
            todo.append((next_ifd, is_ifd0))
    return found, orientation


def embedded_jpeg(path: Path | str) -> tuple[bytes, int] | None:
    """The largest JPEG preview embedded in a raw file, with the raw's EXIF orientation.

    Works for TIFF-based raws (NEF, DNG, ARW, PEF, ORF, RW2, CR2, ...) and Fuji RAF. Returns
    None when there is none (or the file is unreadable). Never raises.
    """
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            head = _read(f, 0, 16)
            if head.startswith(b"FUJIFILMCCD-RAW"):
                off, length = struct.unpack(">II", _read(f, 84, 8))
                candidates, orientation = [(off, length)], 1
            else:
                candidates, orientation = _tiff_previews(f, size)
            best: bytes | None = None
            for off, length in sorted(candidates, key=lambda c: -c[1]):
                if not 0 < length <= MAX_EMBEDDED or off + length > size:
                    continue
                if _read(f, off, 2) != b"\xff\xd8":
                    continue
                best = _read(f, off, length)
                break
            return (best, orientation) if best else None
    except (OSError, EOFError, struct.error, ValueError):
        return None


# ---- decoding --------------------------------------------------------------------------------


@dataclass
class Preview:
    image: Any                 # a PIL.Image.Image, already oriented and scaled down
    width: int                 # full size of the source image, upright
    height: int
    source: str                # "jpg", "raw (embedded preview)", ...


def _orientation_of(im: Any) -> int:
    try:
        return int(im.getexif().get(0x0112, 1) or 1)
    except Exception:
        return 1


def _finish(im: Any, max_side: int, orientation: int, source: str) -> Preview:
    Image = pillow()
    w, h = im.size
    try:
        im.draft("RGB", (max_side, max_side))      # JPEG: decode at 1/2, 1/4 or 1/8 size
    except Exception:
        pass
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS if max_side > 400
                 else Image.Resampling.BILINEAR)
    method = _ORIENT.get(orientation)
    if method:
        im = im.transpose(getattr(Image.Transpose, method))
        if orientation in (5, 6, 7, 8):
            w, h = h, w
    return Preview(im, w, h, source)


def load_preview(path: Path | str, max_side: int) -> Preview | None:
    """A preview of ``path`` no larger than ``max_side`` pixels, or None.

    None when Pillow is missing, the format has no preview (video, CR3, HEIF without
    pillow-heif) or the file cannot be decoded. Never raises.
    """
    Image = pillow()
    if Image is None:
        return None
    p = Path(path)
    _, ext = split_ext(p.name)
    try:
        if ext in JPEG_EXTS or ext in PLAIN_IMAGE_EXTS or (ext in HEIF_EXTS
                                                         and can_decode_heif()):
            im = Image.open(p)
            return _finish(im, max_side, _orientation_of(im), ext)
        emb = embedded_jpeg(p)
        if emb is None:
            return None
        data, orientation = emb
        im = Image.open(io.BytesIO(data))
        if orientation == 1:
            orientation = _orientation_of(im)
        return _finish(im, max_side, orientation, "raw (embedded preview)")
    except Exception:
        return None
