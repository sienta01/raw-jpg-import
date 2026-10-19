"""File categories (RAW / JPG / MOV / ...) and the scan skip rules."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

Role = Literal["primary", "sidecar", "ignore"]
ROLES: tuple[str, ...] = ("primary", "sidecar", "ignore")
UnknownPolicy = Literal["report", "copy"]


@dataclass(frozen=True)
class Category:
    key: str                      # "raw", "jpg", "video", ...
    folder: str | None            # subfolder of the event folder; None for sidecar/ignore
    role: Role
    rank: int
    extensions: frozenset[str]    # casefolded, no dot

    @property
    def is_primary(self) -> bool:
        return self.role == "primary"


def split_ext(name: str) -> tuple[str, str]:
    """``("DSC_0001", "nef")`` for ``DSC_0001.NEF``; the extension is casefolded, no dot.

    Names without a dot (or dotfiles like ``.foo``) have an empty extension.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return name, ""
    return stem, ext.casefold()


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex over casefolded POSIX paths.

    ``**`` matches any number of path segments, ``*`` and ``?`` stay within one segment.
    A trailing ``/**`` also matches the directory itself, so a scanner can prune it.
    """
    p = pattern.casefold().strip("/")
    tail = ""
    if p.endswith("/**"):
        p, tail = p[:-3], "(?:/.*)?"
    out: list[str] = []
    i = 0
    while i < len(p):
        if p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out) + tail + r"\Z")


@dataclass(frozen=True)
class FormatTable:
    categories: tuple[Category, ...]
    skip_dirs: tuple[str, ...] = ()
    skip_paths: tuple[str, ...] = ()
    skip_files: tuple[str, ...] = ()
    unknown_policy: UnknownPolicy = "report"
    other_folder: str = "OTHER"
    by_ext: Mapping[str, Category] = field(init=False, repr=False, compare=False)
    _path_res: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_ext: dict[str, Category] = {}
        for cat in self.categories:
            for ext in cat.extensions:
                by_ext[ext] = cat
        object.__setattr__(self, "by_ext", by_ext)
        object.__setattr__(self, "_path_res", tuple(glob_to_regex(p) for p in self.skip_paths))

    def category(self, key: str) -> Category | None:
        for cat in self.categories:
            if cat.key == key:
                return cat
        return None

    def classify(self, name: str) -> Category | None:
        """The category for a file name, or ``None`` for an unknown extension."""
        _, ext = split_ext(name)
        return self.by_ext.get(ext) if ext else None

    def folders(self) -> list[str]:
        """Destination subfolders in rank order (deduplicated), for table columns."""
        seen: list[str] = []
        for cat in sorted(self.categories, key=lambda c: c.rank):
            if cat.role == "primary" and cat.folder and cat.folder not in seen:
                seen.append(cat.folder)
        return seen

    def skip_dir(self, name: str, rel: PurePosixPath) -> bool:
        """True if the directory ``name`` at ``rel`` (relative to the source root) is skipped."""
        n = name.casefold()
        if n.startswith("."):
            return True
        if any(fnmatch.fnmatchcase(n, p.casefold()) for p in self.skip_dirs):
            return True
        r = rel.as_posix().casefold()
        return any(rx.match(r) for rx in self._path_res)

    def skip_file(self, name: str, rel: PurePosixPath) -> bool:
        """True if the file ``name`` at ``rel`` is junk that is skipped silently."""
        n = name.casefold()
        if any(fnmatch.fnmatchcase(n, p.casefold()) for p in self.skip_files):
            return True
        r = rel.as_posix().casefold()
        return any(rx.match(r) for rx in self._path_res)
