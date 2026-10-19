"""Write fake files to disk with a chosen mtime, as a camera would leave them on a card."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def mtime_ns(when: datetime) -> int:
    """Nanoseconds since the epoch for a naive LOCAL datetime (aware ones are honoured)."""
    return int(when.timestamp()) * 1_000_000_000 + when.microsecond * 1000


def write_file(path: Path, data: bytes, mtime: datetime | None = None) -> Path:
    """Create parent folders, write ``data`` and set atime/mtime to ``mtime`` if given.

    A naive ``mtime`` is local wall-clock time, which is what FAT/exFAT cards store.
    For a video use ``fakes.bmff.video_mtime(start, duration)``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        ns = mtime_ns(mtime)
        os.utime(path, ns=(ns, ns))
    return path
