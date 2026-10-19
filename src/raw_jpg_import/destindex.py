"""What already exists in the destination event folders.

Answers these questions for the planner, with one directory listing per folder (cached):

- ``listing``: which names exist in ``<event>/<folder>``?
- ``same_file``: is a destination file the same as a card file (so it can be skipped)?
- ``stem_owners``: which shots already own a stem such as ``DSC_0001`` in an event folder?
- ``numbered_stems``: which renamed stems (``DSC_0001-2``, ...) does an event folder use?
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .fsops import is_online_only
from .grouping import EventTarget
from .ledger import Ledger, root_key
from .models import Capture, SourceFile
from .naming import key
from .shots import same_shot

MTIME_TOLERANCE_NS = 2_000_000_000   # FAT stores mtimes with 2 s resolution
DST_SHIFT_NS = 3600 * 1_000_000_000  # a daylight-saving change moves FAT times by an hour
SAMPLE = 64 * 1024                   # bytes compared at the start, middle and end

_VARIANT = re.compile(r"-v\d+$", re.IGNORECASE)
_NUMBERED = re.compile(r"(.+)-(\d+)")


@dataclass(frozen=True)
class DestFile:
    path: Path
    size: int
    mtime_ns: int
    online_only: bool


def library_stem(name: str) -> str:
    """Stem key a library file counts towards: the part before the first dot, minus ``-vN``."""
    return key(_VARIANT.sub("", name.split(".", 1)[0]))


def _same_samples(a: Path, b: Path, size: int) -> bool:
    """True when two files of ``size`` bytes hold the same bytes at the start, middle and end.

    Both are only opened for reading; any error means "not the same".
    """
    offsets = sorted({0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)})
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            for off in offsets:
                fa.seek(off)
                fb.seek(off)
                if fa.read(SAMPLE) != fb.read(SAMPLE):
                    return False
    except OSError:
        return False
    return True


def _default_reader(path: Path) -> Capture:
    from . import metadata

    return metadata.read_capture(path)


class DestIndex:
    def __init__(self, dest_root: Path, ledger: Ledger | None = None,
                 read_capture: Callable[[Path], Capture] = _default_reader) -> None:
        self.dest_root = dest_root
        self._root_key = root_key(dest_root)
        self._ledger = ledger
        self._read_capture = read_capture
        self._listings: dict[tuple[str, str], dict[str, DestFile]] = {}
        self._subfolders: dict[str, list[str]] = {}
        self._ledger_caps: dict[str, dict[str, Capture]] = {}
        self._read_cache: dict[Path, Capture | None] = {}
        self._stems: dict[str, dict[str, list[DestFile]]] = {}
        self._families: dict[str, dict[str, list[tuple[int, str]]]] = {}

    # -- listings ---------------------------------------------------------------------------

    def listing(self, target: EventTarget, folder: str) -> dict[str, DestFile]:
        """``key(name) -> DestFile`` for regular files in ``<target>/<folder>``."""
        cache_key = (key(target.folder), key(folder))
        cached = self._listings.get(cache_key)
        if cached is not None:
            return cached
        result: dict[str, DestFile] = {}
        if target.exists:
            try:
                with os.scandir(target.path / folder) as it:
                    for entry in it:
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        result[key(entry.name)] = DestFile(
                            Path(entry.path), st.st_size, st.st_mtime_ns, is_online_only(st))
            except (FileNotFoundError, NotADirectoryError):
                pass
            except OSError:
                pass
        self._listings[cache_key] = result
        return result

    def subfolders(self, target: EventTarget) -> list[str]:
        """Names of the subfolders of an event folder (RAW, JPG, ...)."""
        k = key(target.folder)
        if k not in self._subfolders:
            names: list[str] = []
            if target.exists:
                try:
                    with os.scandir(target.path) as it:
                        names = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
                except OSError:
                    names = []
            self._subfolders[k] = names
        return self._subfolders[k]

    # -- comparisons ------------------------------------------------------------------------

    def capture_of(self, d: DestFile, target: EventTarget) -> Capture | None:
        """Capture of a library file: from the ledger if recorded, else read from the file."""
        rel = d.path.relative_to(target.path).as_posix()
        caps = self._ledger_captures(target)
        if (cap := caps.get(key(rel))) is not None:
            return cap
        if d.online_only:
            return None
        return self.own_capture(d.path)

    def own_capture(self, path: Path) -> Capture | None:
        """Capture read from the library file itself (cached); ``None`` if unreadable.

        The caller makes sure the file is not an online-only placeholder.
        """
        if path not in self._read_cache:
            try:
                self._read_cache[path] = self._read_capture(path)
            except Exception:
                self._read_cache[path] = None
        return self._read_cache[path]

    def same_file(self, d: DestFile, f: SourceFile, cap: Capture,
                  target: EventTarget | None = None) -> bool:
        """True when ``d`` is a copy of card file ``f``.

        Sizes must match, and either the mtimes are within 2 s or both carry the same exact
        capture time. Without an exact capture, mtimes exactly one hour apart also match when
        sampled bytes of both files are equal: FAT and exFAT store local time, and after a
        daylight-saving change the OS reports the card's times an hour off. Anything else
        counts as a different file: the import then copies under a new name instead of
        skipping, so nothing is ever lost.
        """
        if d.size != f.size:
            return False
        delta = abs(d.mtime_ns - f.mtime_ns)
        if delta <= MTIME_TOLERANCE_NS:
            return True
        if d.online_only:
            return False
        if not cap.exact:
            return abs(delta - DST_SHIFT_NS) <= MTIME_TOLERANCE_NS and \
                _same_samples(d.path, f.path, f.size)
        other = self.capture_of(d, target) if target is not None else self._read_direct(d)
        return other is not None and other.exact and same_shot(other, cap)

    def _read_direct(self, d: DestFile) -> Capture | None:
        try:
            return self._read_capture(d.path)
        except Exception:
            return None

    def stem_owners(self, target: EventTarget, stem_key: str) -> list[tuple[Path, Capture | None]]:
        """Library files in any subfolder of ``target`` whose stem (minus ``-vN``) is ``stem_key``.

        The capture is ``None`` when it cannot be determined (online-only or unreadable).
        """
        owners = [(d.path, self.capture_of(d, target))
                  for d in self._stem_index(target).get(stem_key, [])]
        owners.sort(key=lambda o: str(o[0]))
        return owners

    def numbered_stems(self, target: EventTarget, stem_key: str) -> list[tuple[int, str]]:
        """``(N, stem key)`` for the stems ``<stem_key>-N`` (N >= 2) used in ``target``, by N."""
        k = key(target.folder)
        families = self._families.get(k)
        if families is None:
            families = defaultdict(list)
            for sk in self._stem_index(target):
                m = _NUMBERED.fullmatch(sk)
                if m and int(m.group(2)) >= 2:
                    families[m.group(1)].append((int(m.group(2)), sk))
            for numbered in families.values():
                numbered.sort()
            self._families[k] = families
        return list(families.get(stem_key, []))

    def _stem_index(self, target: EventTarget) -> dict[str, list[DestFile]]:
        """Stem key (see :func:`library_stem`) -> library files of ``target`` using it."""
        k = key(target.folder)
        index = self._stems.get(k)
        if index is None:
            index = defaultdict(list)
            for folder in self.subfolders(target):
                for name_key, d in self.listing(target, folder).items():
                    index[library_stem(name_key)].append(d)
            self._stems[k] = index
        return index

    # -- ledger -----------------------------------------------------------------------------

    def _ledger_captures(self, target: EventTarget) -> dict[str, Capture]:
        k = key(target.folder)
        if k in self._ledger_caps:
            return self._ledger_caps[k]
        caps: dict[str, Capture] = {}
        if self._ledger is not None:
            for row in self._ledger.rows_for_event(self._root_key, target.folder):
                if not row.capture:
                    continue
                try:
                    local = datetime.fromisoformat(row.capture)
                except ValueError:
                    continue
                caps[key(row.dest_rel)] = Capture(
                    local=local, source="ledger", exact=bool(row.capture_exact),
                    subsec_ms=row.subsec)
        self._ledger_caps[k] = caps
        return caps
