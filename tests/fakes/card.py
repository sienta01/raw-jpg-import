"""Fake camera cards on disk, laid out like a Nikon Zf's two slots, plus import helpers.

- :class:`FakeShot` / :class:`FakeCard` / :func:`make_zf_cards` build card folders
  (``DCIM/100NCZ_F/DSC_0001.NEF`` ...) whose NEF, JPG and HIF files of one shot carry the
  same exact EXIF time and SubSecTimeOriginal, as the real camera writes them.
- :func:`zf_cards` builds the standard scenario (the sunset walk) used across the tests.
- :func:`scan`, :func:`make_plan` and :func:`run_import` drive the engine like the CLI does.
- :class:`FaultyFileOps` injects faults into the copier and the deleter: read errors,
  corrupted or failing writes (ENOSPC, EFBIG), a pulled card, busy or link-less renames,
  a crash right after a rename, and hooks to cancel mid-file (:func:`cancel_after`).

Nothing here ever touches a real card: call :func:`no_real_cards` (tests) so card detection
only sees an empty list of volumes.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import random
import stat
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, BinaryIO, Literal

from fakes.bmff import make_heif, make_mov, video_mtime
from fakes.files import write_file
from fakes.jpeg import make_jpeg, seeded_bytes, size_for
from fakes.raw import make_nef
from raw_jpg_import.fsops import RealFileOps

Quality = Literal["RAW", "JPG", "RAW+JPG", "HIF", "RAW+HIF", "MOV"]
Mode = Literal["overflow", "backup", "raw_jpg", "jpg_jpg", "raw_heif"]
MODES: tuple[str, ...] = ("overflow", "backup", "raw_jpg", "jpg_jpg", "raw_heif")

AUTO = "auto"
ZF_TZ_MINUTES = 7 * 60                   # the camera's time zone (UTC+07:00)
NEF_BASE_SIZE = 96 * 1024
BIG_FILE = 8 * 1024 * 1024               # videos above this are streamed to disk
STREAM_CHUNK = 4 * 1024 * 1024

_EXTS: dict[str, tuple[str, ...]] = {
    "RAW": ("NEF",), "JPG": ("JPG",), "RAW+JPG": ("NEF", "JPG"), "HIF": ("HIF",),
    "RAW+HIF": ("NEF", "HIF"), "MOV": ("MOV",),
}
DEST_FOLDER = {"NEF": "RAW", "JPG": "JPG", "HIF": "JPG", "MOV": "MOV"}
# What FakeCard.junk() writes (with the default folder), relative to the card root.
JUNK_FILES = frozenset({
    "$RECYCLE.BIN/S-1-5-21-1/$R1A2B3C.JPG", "System Volume Information/IndexerVolumeGuid",
    ".Trashes/501/DSC_9998.NEF", "DCIM/100NCZ_F/._DSC_0001.NEF", "DCIM/100NCZ_F/NC_FLLST.DAT",
    "NIKON001.DSC",
})


@dataclass(frozen=True)
class FakeShot:
    """One press of the shutter (or one clip) on the camera."""

    stem: str
    when: datetime                       # camera wall-clock time (EXIF DateTimeOriginal)
    quality: Quality = "RAW+JPG"
    folder: str = "100NCZ_F"
    subsec: str | None = AUTO            # SubSecTimeOriginal; AUTO = two seeded digits
    duration_s: float = 12.0             # MOV only
    protect: bool = False                # the camera's protect flag on the shot's NEF

    @property
    def subsec_text(self) -> str | None:
        if self.subsec != AUTO:
            return self.subsec
        return f"{random.Random(f'{self.stem}@{self.when.isoformat()}').randrange(100):02d}"

    @property
    def exts(self) -> tuple[str, ...]:
        return _EXTS[self.quality]

    @property
    def seed(self) -> str:
        return f"{self.stem}@{self.when.isoformat()}"


def sha256_file(p: Path) -> str:
    with open(p, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class FakeCard:
    """A card (or folder) on disk with a ``DCIM`` folder, written like a camera would."""

    def __init__(self, root: Path, label: str | None = None) -> None:
        self.root = Path(root)
        self.label = label or self.root.name
        (self.root / "DCIM").mkdir(parents=True, exist_ok=True)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def find(self, name: str) -> Path:
        """The first file called ``name`` anywhere on the card (or ``name`` as a rel path)."""
        if "/" in name:
            return self.path(name)
        for p in sorted(self.root.rglob(name)):
            if p.is_file():
                return p
        raise FileNotFoundError(name)

    # -- writing ----------------------------------------------------------------------------

    def photo(self, name: str, when: datetime, *, folder: str = "100NCZ_F",
              subsec: str | None = None, size: int | None = None, seed: str | None = None,
              mtime: datetime | None = None) -> Path:
        """Write a NEF, JPG or HIF with DateTimeOriginal ``when`` and mtime ``when``."""
        ext = name.rpartition(".")[2].casefold()
        seed = seed or f"{name}@{when.isoformat()}"
        if ext == "nef":
            data = make_nef(when, name=seed, size=size_for(seed, size, NEF_BASE_SIZE),
                            subsec=subsec)
        elif ext in ("jpg", "jpeg"):
            data = make_jpeg(when, name=seed, size=size, subsec=subsec)
        elif ext in ("hif", "heif", "heic"):
            data = make_heif(when, name=seed, size=size, subsec=subsec)
        else:
            raise ValueError(f"not a photo: {name}")
        return write_file(self.path(f"DCIM/{folder}/{name}"), data, mtime or when)

    def video(self, name: str, start: datetime, *, duration_s: float = 12.0,
              folder: str = "100NCZ_F", tz_minutes: int = ZF_TZ_MINUTES,
              size: int | None = None, seed: str | None = None) -> Path:
        """Write a Zf MOV (NCTG local start time; mtime = end of the clip, as on FAT)."""
        seed = seed or f"{name}@{start.isoformat()}"
        path = self.path(f"DCIM/{folder}/{name}")
        end = video_mtime(start, duration_s)
        if size is None or size <= BIG_FILE:
            return write_file(path, make_mov(start, tz_minutes, duration_s, name=seed,
                                             size=size), end)
        # Large clip: moov first, then an mdat of size 0 ("to end of file") streamed to disk.
        head = make_mov(start, tz_minutes, duration_s, name=seed, size=64 * 1024,
                        moov_at_end=False, mdat_size_zero=True)
        write_file(path, head)
        rng = random.Random(seed)
        left = size - len(head)
        with open(path, "ab") as f:
            while left > 0:
                n = min(STREAM_CHUNK, left)
                f.write(rng.randbytes(n))
                left -= n
        ns = int(end.timestamp()) * 1_000_000_000
        os.utime(path, ns=(ns, ns))
        return path

    def file(self, rel: str, data: bytes, mtime: datetime | None = None) -> Path:
        return write_file(self.path(rel), data, mtime)

    def unknown(self, name: str = "NOTES.XYZ", *, folder: str = "100NCZ_F",
                when: datetime | None = None) -> Path:
        """A file of a type the tool does not know (reported, or copied into OTHER/)."""
        return write_file(self.path(f"DCIM/{folder}/{name}"),
                          b"unknown " + seeded_bytes(name, 500), when)

    def junk(self, folder: str = "100NCZ_F") -> list[Path]:
        """System and camera files that must never be imported nor touched (JUNK_FILES)."""
        return [
            write_file(self.path("$RECYCLE.BIN/S-1-5-21-1/$R1A2B3C.JPG"),
                       make_jpeg(datetime(2025, 1, 1), name="recycled")),
            write_file(self.path("System Volume Information/IndexerVolumeGuid"),
                       "{1a2b3c4d-0000-0000-0000-000000000000}".encode("utf-16")),
            write_file(self.path(".Trashes/501/DSC_9998.NEF"), make_nef(datetime(2025, 1, 1))),
            write_file(self.path(f"DCIM/{folder}/._DSC_0001.NEF"),
                       b"\x00\x05\x16\x07" + bytes(60)),
            write_file(self.path(f"DCIM/{folder}/NC_FLLST.DAT"), seeded_bytes("fllst", 2048)),
            write_file(self.path("NIKON001.DSC"), seeded_bytes("nikon-dsc", 128)),
        ]

    def protect(self, name: str) -> Path:
        """Set the camera's "protect" flag (read-only) on a file."""
        p = self.find(name)
        os.chmod(p, stat.S_IREAD)
        return p

    def unprotect_all(self) -> None:
        for p in self.root.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IREAD | stat.S_IWRITE)

    # -- inspecting -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, tuple[int, int, str]]:
        """``{rel: (size, mtime_ns, sha256)}`` for every file on the card, junk included."""
        out: dict[str, tuple[int, int, str]] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[p.relative_to(self.root).as_posix()] = (st.st_size, st.st_mtime_ns,
                                                            sha256_file(p))
        return out

    def dirs(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_dir()}

    def protected(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                if p.is_file() and not os.stat(p).st_mode & stat.S_IWRITE}

    def state(self) -> tuple[dict[str, tuple[int, int, str]], set[str], set[str]]:
        """Everything that must stay unchanged: file contents and times, folders, flags."""
        return self.snapshot(), self.dirs(), self.protected()


# ---- two-slot Zf cards ---------------------------------------------------------------------


def _slots_for(mode: str, ext: str, index: int, count: int) -> list[tuple[int, str]]:
    """``[(slot, variant)]`` for one file of shot number ``index``: which cards record it."""
    if mode == "backup":
        return [(1, ""), (2, "")]
    if mode in ("raw_jpg", "raw_heif"):
        return [(1, "")] if ext == "NEF" else [(2, "")]
    if mode == "overflow":
        return [(1, "")] if index < (count + 1) // 2 else [(2, "")]
    if mode == "jpg_jpg":
        return [(1, "")] if ext == "NEF" else [(1, ""), (2, "basic")]
    raise ValueError(f"unknown mode {mode!r}")


def make_zf_cards(tmp: Path, shots: Sequence[FakeShot], mode: Mode = "raw_jpg", *,
                  video_slot: int = 1, junk: bool = True,
                  names: tuple[str, str] = ("SLOT1", "SLOT2"),
                  big_video: int | None = None) -> tuple[Path, Path]:
    """Write ``shots`` onto two cards as a Zf in ``mode`` would; return the two card roots.

    Modes: ``raw_jpg`` (NEF slot 1, JPG/HIF slot 2), ``raw_heif`` (same, for RAW+HIF),
    ``backup`` (every file on both cards, identical), ``overflow`` (first half of the shots
    on slot 1, the rest on slot 2), ``jpg_jpg`` (JPG on both, a smaller "basic" one on
    slot 2). Videos go to ``video_slot`` (both cards in backup mode). ``big_video`` makes
    the first clip that many bytes.
    """
    cards = {1: FakeCard(Path(tmp) / names[0], "NIKON Z F"),
             2: FakeCard(Path(tmp) / names[1], "NIKON Z F")}
    for index, shot in enumerate(shots):
        if shot.quality == "MOV":
            slots = [1, 2] if mode == "backup" else [video_slot]
            size, big_video = big_video, None
            for slot in slots:
                cards[slot].video(f"{shot.stem}.MOV", shot.when, duration_s=shot.duration_s,
                                  folder=shot.folder, seed=shot.seed, size=size)
            continue
        for ext in shot.exts:
            for slot, variant in _slots_for(mode, ext, index, len(shots)):
                seed = f"{shot.seed}.{ext}{variant}"
                size = None
                if variant:
                    size = size_for(seed, None) // 2
                cards[slot].photo(f"{shot.stem}.{ext}", shot.when, folder=shot.folder,
                                  subsec=shot.subsec_text, seed=seed, size=size)
                if shot.protect and ext == "NEF":
                    cards[slot].protect(f"DCIM/{shot.folder}/{shot.stem}.{ext}")
    if junk:
        cards[1].junk()
        cards[2].junk()
    return cards[1].root, cards[2].root


def expected_files(shots: Sequence[FakeShot], mode: str = "raw_jpg") -> set[str]:
    """Library files (``RAW/DSC_0001-2.NEF`` ...) for ``shots`` imported into one folder.

    An independent oracle: shots in time order; a stem used again gets ``-2``, ``-3``.
    """
    used: Counter[str] = Counter()
    out: set[str] = set()
    for shot in sorted(shots, key=lambda s: s.when):
        used[shot.stem.casefold()] += 1
        n = used[shot.stem.casefold()]
        stem = shot.stem if n == 1 else f"{shot.stem}-{n}"
        for ext in shot.exts:
            out.add(f"{DEST_FOLDER[ext]}/{stem}.{ext}")
            if mode == "jpg_jpg" and ext == "JPG":
                out.add(f"JPG/{stem}-v2.JPG")
    return out


# ---- the standard scenario: a sunset walk past midnight -----------------------------------

EVENT_NAME = "Sunset Beachwalk"
EVENT = "2026.01.23 - Sunset Beachwalk"
DAY1 = date(2026, 1, 23)
DAY2 = date(2026, 1, 24)
DAY_START = time(1, 0)      # keeps the walk past midnight on the 23rd


def sunset_shots() -> list[FakeShot]:
    """The walk 23:02-00:31 plus an earlier DSC_0001 (18:00) that the counter reuses at 23:40."""
    d = datetime
    return [
        FakeShot("DSC_0001", d(2026, 1, 23, 18, 0, 0)),
        FakeShot("DSC_0002", d(2026, 1, 23, 23, 2, 10)),
        FakeShot("DSC_0003", d(2026, 1, 23, 23, 15, 42)),
        FakeShot("DSC_0004", d(2026, 1, 23, 23, 31, 5), protect=True),
        FakeShot("_DSC0005", d(2026, 1, 23, 23, 35, 20)),            # Adobe RGB naming
        FakeShot("DSC_0001", d(2026, 1, 23, 23, 40, 0), folder="101NCZ_F"),   # rollover
        FakeShot("DSC_0010", d(2026, 1, 23, 23, 45, 0), quality="MOV", folder="101NCZ_F"),
        FakeShot("DSC_0006", d(2026, 1, 23, 23, 52, 12), folder="101NCZ_F"),
        FakeShot("DSC_0007", d(2026, 1, 24, 0, 2, 30), folder="101NCZ_F"),
        FakeShot("DSC_0008", d(2026, 1, 24, 0, 15, 0), folder="101NCZ_F"),
        FakeShot("DSC_0009", d(2026, 1, 24, 0, 31, 0), folder="101NCZ_F"),
    ]


@dataclass
class ZfCards:
    slot1: FakeCard
    slot2: FakeCard
    shots: list[FakeShot]
    mode: str

    @property
    def roots(self) -> tuple[Path, Path]:
        return self.slot1.root, self.slot2.root

    @property
    def cards(self) -> tuple[FakeCard, FakeCard]:
        return self.slot1, self.slot2

    def state(self) -> list[Any]:
        return [c.state() for c in self.cards]

    def expected(self) -> set[str]:
        return expected_files(self.shots, self.mode)


def zf_cards(tmp: Path, *, mode: Mode = "raw_jpg", junk: bool = True,
             names: tuple[str, str] = ("SLOT1", "SLOT2"),
             shots: Sequence[FakeShot] | None = None, unknown: bool = True,
             big_video: int | None = None) -> ZfCards:
    """The sunset walk on two cards: MOV on slot 1, one protected NEF, a ``NOTES.XYZ``."""
    shots = list(shots if shots is not None else sunset_shots())
    r1, r2 = make_zf_cards(tmp, shots, mode, junk=junk, names=names, big_video=big_video)
    slot1 = FakeCard(r1, "NIKON Z F")
    if unknown:
        slot1.unknown("NOTES.XYZ", when=datetime(2026, 1, 23, 23, 3, 0))
    return ZfCards(slot1, FakeCard(r2, "NIKON Z F"), shots, mode)



# ---- driving the engine --------------------------------------------------------------------


def no_real_cards(monkeypatch: Any) -> None:
    """Card detection sees no volumes, so a test can never reach a real card in the reader."""
    from raw_jpg_import import volumes

    monkeypatch.setattr(volumes, "_mounts", lambda: [])


def default_cfg() -> Any:
    from raw_jpg_import.config import load_config

    return load_config(use_user=False)


def scan(*roots: Path, cfg: Any = None) -> Any:
    """``engine.analyze`` of folder sources ``roots`` (in this order)."""
    from raw_jpg_import import engine

    cfg = cfg or default_cfg()
    scanned = [engine.scan_source(engine.make_source(r, order=i), cfg)
               for i, r in enumerate(roots)]
    return engine.analyze(scanned)


def make_plan(source: Any, dest: Path, *, name: str = EVENT_NAME,
              names: Mapping[date, str] | None = None, day_start: time = DAY_START,
              days: Sequence[date] | None = None, joins: Sequence[date] = (),
              merge_all: bool = False, opts: Any = None, cfg: Any = None,
              ledger: Any = None, probe_write: bool = False) -> Any:
    """Plan ``source`` (an Analysis or a sequence of card roots) into ``dest``."""
    from raw_jpg_import import engine
    from raw_jpg_import.grouping import DayChoice

    cfg = cfg or default_cfg()
    analysis = source if hasattr(source, "shots") else scan(*source, cfg=cfg)
    choices = []
    for g in engine.group(analysis, day_start):
        if days is not None and g.day not in days:
            continue
        choices.append(DayChoice(day=g.day, text=(names or {}).get(g.day, name),
                                 join_previous=g.day in joins))
    return engine.plan(analysis, choices, dest_root=Path(dest), day_start=day_start, cfg=cfg,
                       merge_all=merge_all, opts=opts, ledger=ledger, probe_write=probe_write)


def run_import(source: Any, dest: Path, *, ops: Any = None, cancel: Any = None,
               **plan_kw: Any) -> Any:
    """Plan and execute; returns the ImportReport (``report.plan`` is the plan)."""
    from raw_jpg_import import engine
    from raw_jpg_import.events import NEVER_CANCELLED

    plan = make_plan(source, dest, **plan_kw)
    assert not plan.problems, plan.problems
    return engine.execute(plan, ops=ops, cancel=cancel or NEVER_CANCELLED)


def files_under(root: Path) -> set[str]:
    """Relative POSIX paths of every file below ``root`` (empty if it does not exist)."""
    root = Path(root)
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def part_files(root: Path) -> list[Path]:
    return [p for p in Path(root).rglob("*.part")] if Path(root).exists() else []


def forget_history() -> None:
    """Delete the import history (the ledger), as if it were lost."""
    from raw_jpg_import import appdirs

    db = appdirs.ledger_file()
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


# ---- fault injection -----------------------------------------------------------------------


class Crash(BaseException):
    """A simulated kill or power loss: nothing after the point where it is raised runs."""


def _matches(path: Path, pattern: str) -> bool:
    """``*``, a file name, or a trailing part of the path (``SLOT1/DCIM/.../DSC_0001.NEF``)."""
    if pattern == "*":
        return True
    p = Path(path).as_posix()
    return p == pattern or p.endswith("/" + pattern.lstrip("/"))


def _final_of(tmp: Path) -> Path:
    """The destination a copier temp file (``.NAME.rji-abc123.part``) stands for."""
    name = tmp.name
    if name.startswith(".") and name.endswith(".part") and ".rji-" in name:
        return tmp.with_name(name[1:name.rindex(".rji-")])
    return tmp


def _lookup(rules: Mapping[str, int], path: Path) -> str | None:
    for pattern in rules:
        if _matches(path, pattern):
            return pattern
    return None


def cancel_after(token: Any, pattern: str = "*", nbytes: int = 1) -> Callable[[Path, int], None]:
    """An ``on_chunk`` hook that cancels ``token`` once ``nbytes`` of a matching file were read."""
    def hook(path: Path, n: int) -> None:
        if n >= nbytes and _matches(path, pattern):
            token.cancel()

    return hook


def lock_error(winerror: int = 32) -> PermissionError:
    """The PermissionError Windows raises while another process holds a file."""
    if sys.platform == "win32":
        e = OSError(errno.EACCES, "The process cannot access the file (injected)", None,
                    winerror)
    else:
        e = PermissionError(errno.EACCES, "The process cannot access the file (injected)")
        e.winerror = winerror     # type: ignore[attr-defined]
    assert isinstance(e, PermissionError)
    return e


class _Reader:
    def __init__(self, f: BinaryIO, path: Path, ops: FaultyFileOps) -> None:
        self.f, self.path, self.ops, self.n = f, path, ops, 0
        pattern = _lookup(ops.read_error, path)
        self.vanish = ops.vanish if ops.vanish and _matches(path, ops.vanish[0]) else None
        limits = [ops.read_error[pattern]] if pattern is not None else []
        if self.vanish is not None:
            limits.append(self.vanish[2])
        self.limit = min(limits) if limits else None

    def readinto(self, b: Any) -> int:
        view = memoryview(b)
        if self.limit is not None:
            if self.n >= self.limit:
                if self.vanish is not None and self.n >= self.vanish[2]:
                    self.f.close()      # Windows cannot rename a folder with an open file
                    self.ops.vanish_root(self.vanish[1])
                    self.vanish = None
                raise OSError(errno.EIO, "I/O error (injected)", str(self.path))
            view = view[: self.limit - self.n]
        k = self.f.readinto(view) or 0
        self.n += k
        if self.ops.on_chunk is not None and k:
            self.ops.on_chunk(self.path, self.n)
        return k

    def read(self, n: int = -1) -> bytes:
        return self.f.read(n)

    def close(self) -> None:
        self.f.close()

    def __enter__(self) -> _Reader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.f.close()


class _Writer:
    def __init__(self, f: BinaryIO, final: Path, ops: FaultyFileOps, flip: bool) -> None:
        self.f, self.final, self.ops, self.flip = f, final, ops, flip

    def write(self, b: Any) -> int:
        if _lookup(self.ops.enospc, self.final) is not None:
            raise OSError(errno.ENOSPC, "No space left on device (injected)", str(self.final))
        pattern = _lookup(self.ops.write_error, self.final)
        if pattern is not None:
            raise self.ops.write_error[pattern]
        if self.flip and len(b):
            data = bytearray(b)
            data[len(data) // 2] ^= 0xFF
            self.flip = False
            self.ops.flipped += 1
            return self.f.write(data)
        return self.f.write(b)

    def flush(self) -> None:
        self.f.flush()

    def fileno(self) -> int:
        return self.f.fileno()

    def close(self) -> None:
        self.f.close()

    def __enter__(self) -> _Writer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.f.close()


class FaultyFileOps(RealFileOps):
    """:class:`~raw_jpg_import.fsops.RealFileOps` with injected faults.

    Path patterns (``_matches``) select the files a fault applies to:

    - ``read_error``: card file -> raise EIO once that many bytes were read (every attempt);
    - ``flip``: destination -> corrupt one byte in that many copy attempts (verify mismatch);
    - ``enospc``: destinations whose writes fail with ENOSPC (disk full);
    - ``write_error``: destination -> the OSError its writes raise (e.g. EFBIG);
    - ``vanish``: ``(card file, card root, bytes)`` -> after that many bytes of the file the
      card root disappears (renamed away) and the read fails, like a pulled card;
    - ``makedirs_error``: folders whose creation fails with that error;
    - ``rename_busy``: the next N renames fail with winerror 32 (antivirus/OneDrive) first;
    - ``link_eperm``: use the POSIX rename path with ``os.link`` failing with EPERM (exFAT);
    - ``crash_after_rename``: destinations whose rename succeeds and then the process "dies"
      (:class:`Crash`), i.e. before the ledger records the file;
    - ``on_open_read`` / ``on_chunk``: hooks (e.g. to cancel) on opening and per chunk read.
    """

    def __init__(self, *, read_error: Mapping[str, int] | None = None,
                 flip: Mapping[str, int] | None = None, enospc: Sequence[str] = (),
                 write_error: Mapping[str, OSError] | None = None,
                 vanish: tuple[str, Path, int] | None = None,
                 makedirs_error: Mapping[str, OSError] | None = None,
                 rename_busy: int = 0, link_eperm: bool = False,
                 crash_after_rename: Sequence[str] = (),
                 on_open_read: Callable[[Path], None] | None = None,
                 on_chunk: Callable[[Path, int], None] | None = None) -> None:
        super().__init__(sleep=self._no_sleep)
        self.read_error = dict(read_error or {})
        self.flip_left = dict(flip or {})
        self.enospc = dict.fromkeys(enospc, 0)
        self.write_error = dict(write_error or {})
        self.vanish = vanish
        self.makedirs_error = dict(makedirs_error or {})
        self.rename_busy = rename_busy
        self.link_eperm = link_eperm
        self.crash_after_rename = dict.fromkeys(crash_after_rename, 0)
        self.on_open_read = on_open_read
        self.on_chunk = on_chunk
        self.sleeps: list[float] = []
        self.flipped = 0
        self.vanished: list[Path] = []
        self.opened: list[Path] = []
        self.renames = 0

    def _no_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def vanish_root(self, root: Path) -> None:
        gone = root.with_name(root.name + ".removed")
        os.rename(root, gone)
        self.vanished.append(gone)

    # -- reading ----------------------------------------------------------------------------

    def open_read(self, p: Path) -> BinaryIO:
        self.opened.append(Path(p))
        if self.on_open_read is not None:
            self.on_open_read(Path(p))
        f = super().open_read(p)
        return _Reader(f, Path(p), self)  # type: ignore[return-value]

    # -- writing ----------------------------------------------------------------------------

    def open_excl(self, p: Path) -> BinaryIO:
        f = super().open_excl(p)
        final = _final_of(Path(p))
        flip = False
        pattern = _lookup(self.flip_left, final)
        if pattern is not None and self.flip_left[pattern] > 0:
            self.flip_left[pattern] -= 1
            flip = True
        return _Writer(f, final, self, flip)  # type: ignore[return-value]

    def makedirs(self, d: Path) -> list[Path]:
        pattern = _lookup(self.makedirs_error, Path(d))
        if pattern is not None:
            raise self.makedirs_error[pattern]
        return super().makedirs(d)

    def rename_noreplace(self, src: Path, dst: Path) -> None:
        self.renames += 1
        with self._patched_rename():
            super().rename_noreplace(src, dst)
        if _lookup(self.crash_after_rename, Path(dst)) is not None:
            raise Crash(f"simulated crash after renaming {Path(dst).name}")

    @contextlib.contextmanager
    def _patched_rename(self) -> Iterator[None]:
        """Swap ``os.rename``/``os.link`` (as fsops sees them) for one rename call."""
        from raw_jpg_import import fsops

        if not self.rename_busy and not self.link_eperm:
            yield
            return
        real_rename, real_link, real_platform = os.rename, getattr(os, "link", None), \
            fsops._platform
        # Lock errors only exist on the Windows path; EPERM links only on the POSIX one.
        platform = "linux" if self.link_eperm else "win32"

        def busy_rename(a: Any, b: Any) -> None:
            if self.rename_busy > 0:
                self.rename_busy -= 1
                raise lock_error(32)
            real_rename(a, b)

        def no_link(a: Any, b: Any) -> None:
            raise PermissionError(errno.EPERM, "Operation not permitted (injected)", str(a))

        fsops.os.rename = busy_rename           # type: ignore[assignment]
        fsops._platform = lambda: platform      # type: ignore[assignment]
        if self.link_eperm:
            fsops.os.link = no_link             # type: ignore[assignment]
        try:
            yield
        finally:
            fsops.os.rename = real_rename       # type: ignore[assignment]
            if real_link is not None:
                fsops.os.link = real_link       # type: ignore[assignment]
            fsops._platform = real_platform     # type: ignore[assignment]
