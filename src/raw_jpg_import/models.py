"""Core value types shared across the engine: sources, scanned files and capture times."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from .formats import Category

SourceKind = Literal["card", "folder"]


@dataclass(frozen=True)
class Source:
    """A card or folder being imported from.

    ``order`` is the position in the user's source list; lower wins when the same
    file exists on two cards (backup mode).
    """

    root: Path
    label: str
    serial: str | None = None
    kind: SourceKind = "folder"
    order: int = 0

    @property
    def key(self) -> str:
        """Stable identity for caches: serial (or label) plus root path."""
        return f"{self.serial or self.label}|{self.root}"


@dataclass(frozen=True)
class SourceFile:
    """One file found on a source by the scanner."""

    source: Source
    path: Path                 # absolute path on the card
    rel: PurePosixPath         # relative to source.root, POSIX separators
    name: str                  # file name as on the card, e.g. "DSC_0001.NEF"
    stem: str                  # "DSC_0001"
    ext: str                   # casefolded, no dot: "nef" ("" if none)
    size: int
    mtime_ns: int
    readonly: bool             # camera "protect" flag (read-only attribute)
    category: Category | None  # None = unknown extension

    @property
    def orig_ext(self) -> str:
        """The extension with its original case, without the dot ("NEF")."""
        _, dot, ext = self.name.rpartition(".")
        return ext if dot and self.name[: -len(ext) - 1] else ""


CaptureSource = Literal[
    "exif", "heif-exif", "cr3", "raf", "nctg", "qt-keys",
    "mvhd+mtime", "mvhd-pc-tz", "mtime", "partner",
]

EXACT_SOURCES = frozenset({"exif", "heif-exif", "cr3", "raf", "nctg", "qt-keys"})


@dataclass(frozen=True)
class Capture:
    """When a photo or video was taken, as camera wall-clock time.

    ``local`` is NAIVE (no tzinfo): the time shown on the camera's clock.
    ``exact`` is True only when read from embedded metadata (EXIF, NCTG, QuickTime keys).
    """

    local: datetime
    source: str
    exact: bool
    subsec_ms: int | None = None
    offset: str | None = None      # e.g. "+07:00" when the camera recorded it
    suspicious: bool = False       # year < 1995 or more than a year in the future
