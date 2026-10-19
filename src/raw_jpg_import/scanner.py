"""Walk a source (card or folder) and classify its files.

The walk only lists and stats; it never opens a file. Cards are scanned from the volume
root because some cameras keep video outside DCIM (Sony: ``PRIVATE/M4ROOT/CLIP``).
"""

from __future__ import annotations

import os
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .events import NEVER_CANCELLED, CancelToken, Emit, Progress, Throttle, no_emit
from .formats import FormatTable, split_ext
from .fsops import is_readonly
from .models import Source, SourceFile
from .volumes import no_error_dialogs

CANCEL_EVERY = 200      # files between cancel checks inside one directory
JUNK = "junk"           # `ignored` key for files dropped by the skip rules
SYMLINK = "symlink"     # `ignored` key for symbolic links and junctions (never followed)
_NAME_SURROGATE = 0x20000000    # IsReparseTagNameSurrogate: junction, mount point, symlink


@dataclass
class SourceScan:
    source: Source
    files: list[SourceFile] = field(default_factory=list)   # primary, sidecar and unknown
    unknown: dict[str, list[Path]] = field(default_factory=dict)  # ext ("" if none) -> paths
    ignored: Counter[str] = field(default_factory=Counter)  # ext/name/"junk" -> count
    errors: list[tuple[Path, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        """Sum of the sizes of ``files`` (ignored and junk files are not counted)."""
        return sum(f.size for f in self.files)


def _is_dir_link(entry: os.DirEntry[str]) -> bool:
    """Windows junctions and folder mount points: links that ``is_symlink()`` does not report.

    Only "name surrogate" reparse points redirect to another path; cloud-file placeholders
    (OneDrive folders) are reparse points too but are real folders and are walked.
    """
    st = entry.stat(follow_symlinks=False)
    if not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return bool(getattr(st, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _top_dir(root: Path) -> Path:
    # A bare "X:" means "the current directory on drive X", not the drive's root.
    if root.drive and not root.root and len(root.parts) == 1:
        return Path(root.drive + os.sep)
    return root


def _always_junk(name: str) -> bool:
    # Dotfiles (including AppleDouble "._*") and our own temp files are never photos,
    # even if a user config drops them from skip_files.
    return name.startswith(".") or name.casefold().endswith(".part")


def _reason(e: OSError) -> str:
    return e.strerror or str(e) or type(e).__name__


def _source_file(src: Source, path: Path, rel: PurePosixPath, st: os.stat_result,
                 fmt: FormatTable) -> SourceFile:
    name = path.name
    stem, ext = split_ext(name)
    return SourceFile(
        source=src, path=path, rel=rel, name=name, stem=stem, ext=ext,
        size=st.st_size, mtime_ns=st.st_mtime_ns, readonly=is_readonly(st),
        category=fmt.classify(name),
    )


def make_file(src: Source, path: Path, fmt: FormatTable, st: os.stat_result) -> SourceFile:
    """Build one SourceFile for ``path`` from a stat result (also used for re-stats)."""
    try:
        rel = PurePosixPath(*path.relative_to(_top_dir(src.root)).parts)
    except ValueError:
        rel = PurePosixPath(path.name)
    return _source_file(src, path, rel, st, fmt)


class _Walk:
    def __init__(self, src: Source, fmt: FormatTable, cancel: CancelToken, emit: Emit) -> None:
        self.scan = SourceScan(src)
        self.fmt = fmt
        self.cancel = cancel
        self.emit = emit
        self.throttle = Throttle()
        self.visited: set[tuple[int, int]] = set()
        self.seen = 0

    def run(self) -> None:
        top = _top_dir(self.scan.source.root)
        try:
            self._first_visit(os.stat(top))
        except OSError as e:
            self._error(top, e)
            return
        stack: list[tuple[str, PurePosixPath]] = [(os.fspath(top), PurePosixPath())]
        while stack:
            self.cancel.check()
            path, rel = stack.pop()
            subdirs: list[tuple[str, PurePosixPath]] = []
            for entry in self._list(path):
                sub = self._entry(entry, rel / entry.name)
                if sub is not None:
                    subdirs.append(sub)
            stack.extend(reversed(subdirs))

    def finish(self) -> SourceScan:
        scan = self.scan
        scan.files.sort(key=lambda f: (f.rel.as_posix().casefold(), f.rel.as_posix()))
        for paths in scan.unknown.values():
            paths.sort(key=lambda p: (str(p).casefold(), str(p)))
        self.emit(Progress(phase="scan", files_done=len(scan.files), files_total=0))
        return scan

    def _first_visit(self, st: os.stat_result) -> bool:
        """Record a directory's identity; False if it was already walked (loop or link)."""
        if not st.st_ino:  # some filesystems report no inode numbers: cannot tell
            return True
        key = (st.st_dev, st.st_ino)
        if key in self.visited:
            return False
        self.visited.add(key)
        return True

    def _error(self, path: Path | str, e: OSError) -> None:
        self.scan.errors.append((Path(path), _reason(e)))

    def _list(self, path: str) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(path) as it:
                return sorted(it, key=lambda e: (e.name.casefold(), e.name))
        except OSError as e:
            self._error(path, e)
            return []

    def _entry(self, entry: os.DirEntry[str],
               rel: PurePosixPath) -> tuple[str, PurePosixPath] | None:
        """Handle one entry; return ``(path, rel)`` for a subdirectory to descend into."""
        try:
            if entry.is_dir(follow_symlinks=False):
                if _is_dir_link(entry):
                    self.scan.ignored[SYMLINK] += 1
                    return None
                return self._dir(entry, rel)
            if entry.is_symlink():
                self.scan.ignored[SYMLINK] += 1
            elif entry.is_file(follow_symlinks=False):
                self._file(entry, rel)
        except OSError as e:
            self._error(entry.path, e)
        return None

    def _dir(self, entry: os.DirEntry[str],
             rel: PurePosixPath) -> tuple[str, PurePosixPath] | None:
        if self.fmt.skip_dir(entry.name, rel):
            return None
        # os.stat, not entry.stat: on Windows a DirEntry carries no inode number.
        if not self._first_visit(os.stat(entry.path)):
            self.scan.warnings.append(f"{rel}: skipped, this folder was already scanned "
                                      "through another path (link or loop)")
            return None
        return entry.path, rel

    def _file(self, entry: os.DirEntry[str], rel: PurePosixPath) -> None:
        self.seen += 1
        if self.seen % CANCEL_EVERY == 0:
            self.cancel.check()
        name = entry.name
        if _always_junk(name) or self.fmt.skip_file(name, rel):
            self.scan.ignored[JUNK] += 1
            return
        cat = self.fmt.classify(name)
        if cat is not None and cat.role == "ignore":
            _, ext = split_ext(name)
            self.scan.ignored[ext or name.casefold()] += 1
            return
        f = _source_file(self.scan.source, Path(entry.path), rel,
                         entry.stat(follow_symlinks=False), self.fmt)
        self.scan.files.append(f)
        if f.category is None:
            self.scan.unknown.setdefault(f.ext, []).append(f.path)
        if f.size == 0:
            self.scan.warnings.append(f"{rel}: empty file (0 bytes), imported anyway")
        if self.throttle.ready():
            self.emit(Progress(phase="scan", files_done=len(self.scan.files), files_total=0,
                               current=rel.as_posix()))


def scan_source(src: Source, fmt: FormatTable, *, cancel: CancelToken = NEVER_CANCELLED,
                emit: Emit = no_emit) -> SourceScan:
    """List every file under ``src.root`` and classify it. Errors are collected, not raised.

    Raises :class:`~raw_jpg_import.errors.Cancelled` if ``cancel`` is set.
    """
    walk = _Walk(src, fmt, cancel, emit)
    with no_error_dialogs():
        walk.run()
    return walk.finish()
