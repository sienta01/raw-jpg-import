"""Capture time of photos and videos, read with small bounded stdlib parsers.

``read_capture`` picks a parser from the file's magic bytes (then its extension):
TIFF-based raws and JPEG -> source "exif", HEIF -> "heif-exif", CR3 -> "cr3", RAF -> "raf",
MOV/MP4 -> "nctg" | "qt-keys" | "mvhd+mtime" | "mvhd-pc-tz" | "mtime". Anything else, and
any failure, falls back to the file's mtime (source "mtime"). It never raises.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from ..events import NEVER_CANCELLED, CancelToken, Emit, Progress, Throttle, no_emit
from ..formats import split_ext
from ..models import Capture, SourceFile
from ..volumes import no_error_dialogs
from . import bmff, jpeg, quicktime, raf, tiff
from ._io import ParseError, RandomAccess
from .tiff import ExifInfo

__all__ = [
    "Capture", "ExifInfo", "capture_from_mtime", "clear_cache", "detect_kind", "exif_capture",
    "is_suspicious", "parse_capture", "read_capture", "read_capture_bytes", "read_many",
]

log = logging.getLogger(__name__)

Kind = Literal["tiff", "jpeg", "raf", "heif", "cr3", "video"]

TIFF_EXTS = frozenset({
    "nef", "nrw", "dng", "arw", "sr2", "srf", "pef", "ptx", "orf", "ori", "rw2", "rwl", "raw",
    "cr2", "3fr", "fff", "iiq", "erf", "mef", "mos", "srw", "kdc", "dcr", "k25", "gpr",
    "tif", "tiff",
})
JPEG_EXTS = frozenset({"jpg", "jpeg", "jpe"})
HEIF_EXTS = frozenset({"hif", "heif", "heic", "avif"})
VIDEO_EXTS = frozenset({"mov", "mp4", "m4v", "3gp"})
_KIND_BY_EXT: dict[str, Kind] = {
    **dict.fromkeys(TIFF_EXTS, "tiff"), **dict.fromkeys(JPEG_EXTS, "jpeg"),
    **dict.fromkeys(HEIF_EXTS, "heif"), **dict.fromkeys(VIDEO_EXTS, "video"),
    "cr3": "cr3", "raf": "raf",
}
_EXIF_PARSERS: dict[str, tuple[Callable[[RandomAccess], ExifInfo | None], str]] = {
    "tiff": (tiff.parse_tiff, "exif"),
    "jpeg": (jpeg.parse_jpeg, "exif"),
    "raf": (raf.parse_raf, "raf"),
    "heif": (bmff.heif_exif, "heif-exif"),
    "cr3": (bmff.cr3_exif, "cr3"),
}

MIN_PLAUSIBLE_YEAR = 1995
_CACHE_MAX = 200_000

_cache: dict[tuple[str, int, int], Capture] = {}
_cache_lock = threading.Lock()


# --- building Captures --------------------------------------------------------------------


def is_suspicious(local: datetime, now: datetime | None = None) -> bool:
    """True when the camera clock looks wrong: before 1995 or over a year in the future."""
    now = datetime.now() if now is None else now
    return local.year < MIN_PLAUSIBLE_YEAR or local > now + timedelta(days=365)


def _local_from_ns(mtime_ns: int, *, precise: bool = False) -> datetime:
    try:
        if precise:
            return datetime.fromtimestamp(mtime_ns / 1_000_000_000)
        return datetime.fromtimestamp(mtime_ns // 1_000_000_000)
    except (OverflowError, OSError, ValueError):
        # Windows refuses timestamps before 1970; such an mtime is bogus anyway.
        try:
            return datetime(1970, 1, 1) + timedelta(seconds=mtime_ns // 1_000_000_000)
        except OverflowError:
            return datetime(1970, 1, 1)


def capture_from_mtime(mtime_ns: int) -> Capture:
    """The file's mtime as local wall-clock time (what FAT/exFAT cards store). Not exact."""
    local = _local_from_ns(mtime_ns)
    return Capture(local=local, source="mtime", exact=False, suspicious=is_suspicious(local))


def exif_capture(info: ExifInfo | None, source: str) -> Capture | None:
    """A Capture from parsed Exif tags; sub-seconds and offset belong to DateTimeOriginal."""
    if info is None:
        return None
    if info.dto is not None:
        return Capture(local=info.dto, source=source, exact=True,
                       subsec_ms=info.subsec_ms, offset=info.offset)
    local = info.dtd or info.date_time
    return Capture(local=local, source=source, exact=True) if local else None


def _finish(cap: Capture) -> Capture:
    local = cap.local.replace(microsecond=0)
    return replace(cap, local=local, suspicious=is_suspicious(local))


# --- dispatch -------------------------------------------------------------------------------


def detect_kind(head: bytes, ext: str) -> Kind | None:
    """Parser for a file: magic bytes first, then the (casefolded, dotless) extension."""
    if jpeg.is_jpeg(head):
        return "jpeg"
    if raf.is_raf(head):
        return "raf"
    if tiff.is_tiff_header(head):
        return "tiff"
    if bmff.is_bmff(head):
        brand = head[8:12] if head[4:8] == b"ftyp" else b""
        if ext == "cr3" or brand == b"crx ":
            return "cr3"
        if ext in HEIF_EXTS:
            return "heif"           # never rejected on brand (the Zf may write "heix")
        if ext in VIDEO_EXTS:
            return "video"
        return "heif" if brand in bmff.HEIF_BRANDS else "video"
    return _KIND_BY_EXT.get(ext)


def parse_capture(ra: RandomAccess, kind: Kind, mtime_ns: int) -> Capture | None:
    """Run one parser. May raise :class:`ParseError`; ``None`` means "nothing usable"."""
    if kind == "video":
        mtime_local = _local_from_ns(mtime_ns, precise=True) if mtime_ns > 0 else None
        cap = quicktime.read_video(ra, mtime_local)
    else:
        parser, source = _EXIF_PARSERS[kind]
        cap = exif_capture(parser(ra), source)
    return _finish(cap) if cap is not None else None


def _capture_of(ra: RandomAccess, name: str, mtime_ns: int) -> Capture | None:
    try:
        kind = detect_kind(ra.read_upto(0, 16), split_ext(name)[1])
        return parse_capture(ra, kind, mtime_ns) if kind is not None else None
    except ParseError as e:
        log.debug("no capture time in %s: %s", name, e)
        return None


def read_capture_bytes(data: bytes, name: str, mtime_ns: int = 0) -> Capture:
    """Like :func:`read_capture` for in-memory data (``name`` gives the extension)."""
    try:
        cap = _capture_of(RandomAccess.from_bytes(data), name, mtime_ns)
    except Exception:
        log.warning("metadata parser failed on %s", name, exc_info=True)
        cap = None
    return cap or capture_from_mtime(mtime_ns)


def read_capture(path: str | os.PathLike[str], *, size: int | None = None,
                 mtime_ns: int | None = None) -> Capture:
    """Capture time of ``path``. Never raises; falls back to the mtime.

    ``size``/``mtime_ns`` default to an ``os.stat`` of ``path`` and key the cache.
    """
    try:
        return _read_capture(path, size, mtime_ns)
    except Exception:
        log.warning("capture time of %s failed", path, exc_info=True)
        return capture_from_mtime(mtime_ns if isinstance(mtime_ns, int) else 0)


def _read_capture(path: str | os.PathLike[str], size: int | None,
                  mtime_ns: int | None) -> Capture:
    if size is None or mtime_ns is None:
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return capture_from_mtime(mtime_ns or 0)
        size = st.st_size if size is None else size
        mtime_ns = st.st_mtime_ns if mtime_ns is None else mtime_ns
    key = (os.fspath(path), size, mtime_ns)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    try:
        with RandomAccess.open(path) as ra:
            cap = _capture_of(ra, Path(path).name, mtime_ns)
    except OSError as e:
        # The card may be busy or gone: answer, but do not cache, so a retry re-reads.
        log.debug("cannot read %s: %s", path, e)
        return capture_from_mtime(mtime_ns)
    except Exception:
        log.warning("metadata parser failed on %s", path, exc_info=True)
        cap = None
    cap = cap or capture_from_mtime(mtime_ns)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = cap
    return cap


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(f: SourceFile) -> Capture | None:
    with _cache_lock:
        return _cache.get((os.fspath(f.path), f.size, f.mtime_ns))


# --- many files -----------------------------------------------------------------------------


def _read_quietly(path: Path, size: int, mtime_ns: int) -> Capture:
    """``read_capture`` on a pool thread: a pulled card fails, it does not pop up a dialog."""
    with no_error_dialogs():
        return read_capture(path, size=size, mtime_ns=mtime_ns)


def read_many(files: Sequence[SourceFile], *, workers: int = 4,
              cancel: CancelToken = NEVER_CANCELLED, emit: Emit = no_emit) -> dict[Path, Capture]:
    """Capture times for ``files``, read in a thread pool to hide USB latency.

    Emits throttled ``Progress(phase="metadata")`` events from the calling thread and
    raises :class:`~raw_jpg_import.errors.Cancelled` when ``cancel`` is set.
    """
    total = len(files)
    out: dict[Path, Capture] = {}
    throttle = Throttle()
    done = 0

    def progress(name: str) -> None:
        if throttle.ready(force=done == total):
            emit(Progress(phase="metadata", files_done=done, files_total=total, current=name))

    cancel.check()
    emit(Progress(phase="metadata", files_done=0, files_total=total))
    todo: list[SourceFile] = []
    for f in files:
        hit = _cached(f)
        if hit is None:
            todo.append(f)
        else:
            out[f.path] = hit
            done += 1
            progress(f.name)
    if not todo:
        return out

    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="rji-metadata")
    try:
        futures: dict[Future[Capture], SourceFile] = {
            pool.submit(_read_quietly, f.path, f.size, f.mtime_ns): f for f in todo
        }
        pending = set(futures)
        while pending:
            cancel.check()
            finished, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for fut in finished:
                f = futures[fut]
                try:
                    out[f.path] = fut.result()
                except Exception:       # read_capture never raises; belt and braces
                    out[f.path] = capture_from_mtime(f.mtime_ns)
                done += 1
                progress(f.name)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return out
