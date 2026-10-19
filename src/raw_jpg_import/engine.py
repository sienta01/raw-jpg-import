"""Facade used by the GUI and the CLI.

Typical flow::

    cfg = engine.load_config()
    sources = [engine.make_source(v.path, order=i, volume=v)
               for i, v in enumerate(engine.detect_cards())]
    scanned = [engine.scan_source(s, cfg) for s in sources]       # walk + read capture times
    analysis = engine.analyze(scanned)                            # pair files into shots
    groups = engine.group(analysis, day_start)                    # one row per day
    plan = engine.plan(analysis, choices, dest_root=..., day_start=..., cfg=cfg)
    report = engine.execute(plan)                                 # copy + verify
    dplan = engine.plan_deletion(report)                          # opt-in
    engine.delete_originals(dplan, confirmed=True)

Each call opens its own ledger connection (sqlite3 connections belong to one thread).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

from . import appdirs, metadata, volumes
from .config import Config
from .config import load_config as _load_config
from .copier import ImportReport
from .copier import execute as _execute
from .deleter import DeletionPlan, DeletionReport
from .deleter import delete_originals as _delete_originals
from .deleter import plan_deletion as _plan_deletion
from .destindex import DestIndex
from .events import NEVER_CANCELLED, CancelToken, Emit, no_emit
from .fsops import FileOps, file_lock
from .grouping import (
    DayChoice,
    DayGroup,
    ExistingEvent,
    find_existing_events,
    group_days,
    resolve_targets,
    suggest_day_start,
)
from .ledger import Ledger, root_key
from .models import Capture, Source, SourceFile
from .planner import Action, ImportPlan, PlanOptions, build_plan
from .report import append_deletion_log, write_session_log
from .scanner import SourceScan
from .scanner import scan_source as _scan_source
from .shots import Shot, build_shots
from .volumes import Volume

log = logging.getLogger(__name__)

__all__ = [
    "Analysis", "ScannedSource", "Volume", "DayChoice", "DayGroup", "ExistingEvent",
    "ImportPlan", "ImportReport", "DeletionPlan", "DeletionReport", "PlanOptions",
    "load_config", "detect_cards", "make_source", "scan_source", "analyze", "group",
    "suggest_day_start", "existing_events", "plan", "execute", "plan_deletion",
    "delete_originals", "import_lock", "parse_day_start",
]


def load_config(path: Path | None = None) -> Config:
    return _load_config(path)


def parse_day_start(text: str) -> time:
    """``"01:00"`` -> ``time(1, 0)``; only 00:00 to 06:00 is allowed."""
    try:
        h, m = (int(x) for x in text.strip().split(":"))
        t = time(h, m)
    except (ValueError, TypeError):
        raise ValueError(f"day start must look like 01:00, got {text!r}") from None
    if t > time(6, 0):
        raise ValueError("day start must be between 00:00 and 06:00")
    return t


# ---- sources --------------------------------------------------------------------------------


def detect_cards() -> list[Volume]:
    return volumes.detect_cards()


def make_source(path: Path | str, *, order: int = 0, volume: Volume | None = None) -> Source:
    """A Source for a card (``volume`` given, or ``path`` is a detected card root) or folder."""
    p = Path(path)
    if volume is None:
        with contextlib.suppress(Exception):
            v = volumes.volume_for_path(p)
            if v is not None and _same_path(v.path, p):
                volume = v
    if volume is not None and _same_path(volume.path, p):
        label = volume.label or str(p)
        return Source(root=p, label=label, serial=volume.serial, kind="card", order=order)
    return Source(root=p, label=p.name or str(p), kind="folder", order=order)


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


@dataclass
class ScannedSource:
    """One scanned source plus the capture times of its files (the GUI caches these)."""
    scan: SourceScan
    captures: dict[Path, Capture]

    @property
    def source(self) -> Source:
        return self.scan.source


def scan_source(src: Source, cfg: Config, *, cancel: CancelToken = NEVER_CANCELLED,
                emit: Emit = no_emit) -> ScannedSource:
    """Walk ``src`` and read the capture time of every photo and video on it."""
    scan = _scan_source(src, cfg.formats, cancel=cancel, emit=emit)
    primaries = [f for f in scan.files if f.category is not None and f.category.is_primary]
    captures = metadata.read_many(primaries, cancel=cancel, emit=emit)
    for f in scan.files:
        if f.path not in captures:
            captures[f.path] = metadata.capture_from_mtime(f.mtime_ns)
    return ScannedSource(scan, captures)


# ---- analysis -------------------------------------------------------------------------------


@dataclass
class Analysis:
    sources: list[Source]
    files: list[SourceFile]
    captures: dict[Path, Capture]
    shots: list[Shot]
    orphans: list[SourceFile]
    unknown_files: list[SourceFile]
    unknown: dict[str, int] = field(default_factory=dict)
    ignored: Counter[str] = field(default_factory=Counter)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


def analyze(scanned: Sequence[ScannedSource]) -> Analysis:
    """Merge scanned sources (in the given order) and pair their files into shots."""
    ordered = sorted(scanned, key=lambda s: s.source.order)
    files: list[SourceFile] = []
    captures: dict[Path, Capture] = {}
    unknown: Counter[str] = Counter()
    ignored: Counter[str] = Counter()
    errors: list[tuple[Path, str]] = []
    warnings: list[str] = []
    for s in ordered:
        files.extend(s.scan.files)
        captures.update(s.captures)
        for ext, paths in s.scan.unknown.items():
            unknown[ext] += len(paths)
        ignored.update(s.scan.ignored)
        errors.extend(s.scan.errors)
        warnings.extend(f"{s.source.label}: {w}" for w in s.scan.warnings)
    shots, orphans = build_shots(files, captures)
    unknown_files = [f for f in files if f.category is None]
    return Analysis(sources=[s.source for s in ordered], files=files, captures=captures,
                    shots=shots, orphans=orphans, unknown_files=unknown_files,
                    unknown=dict(unknown), ignored=ignored, errors=errors, warnings=warnings)


def group(analysis: Analysis, day_start: time) -> list[DayGroup]:
    return group_days(analysis.shots, day_start)


# ---- destination ----------------------------------------------------------------------------


# Messages from opening a history file (e.g. "it was damaged and set aside"), by its path.
# Any engine call may be the first to open it, and the message stays true for the rest of
# the session, so every later plan repeats it (the GUI rebuilds plans all the time).
_history_notes: dict[str, list[str]] = {}

HISTORY_UNAVAILABLE = ("The import history could not be opened; files count as already "
                       "imported only when they are found in the destination folders")


def _history_warnings(led: Ledger | None) -> list[str]:
    if led is None:
        return [HISTORY_UNAVAILABLE]
    return list(_history_notes.get(str(led.path), [])) if led.path is not None else []


@contextlib.contextmanager
def _ledger(ledger: Ledger | None = None) -> Iterator[Ledger | None]:
    if ledger is not None:
        yield ledger
        return
    try:
        led = Ledger.open()
    except Exception as e:     # the ledger only adds knowledge; disk checks still protect
        log.error("import history unavailable: %s", e)
        yield None
        return
    if led.warnings and led.path is not None:
        notes = _history_notes.setdefault(str(led.path), [])
        notes.extend(w for w in led.warnings if w not in notes)
    try:
        yield led
    finally:
        led.close()


def existing_events(dest_root: Path, *, ledger: Ledger | None = None,
                    day_start: time = time(0)) -> list[ExistingEvent]:
    """Event folders already in ``dest_root`` (with the days the history knows they hold).

    Pass the current ``day_start`` so a folder only holds the days its files belong to.
    """
    with _ledger(ledger) as led:
        days = led.days_by_event(root_key(dest_root), day_start) if led is not None else {}
    return find_existing_events(dest_root, days)


def disk_free(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def plan(analysis: Analysis, choices: Sequence[DayChoice], *, dest_root: Path, day_start: time,
         cfg: Config, merge_all: bool = False, opts: PlanOptions | None = None,
         probe_write: bool = False, ledger: Ledger | None = None,
         cancel: CancelToken = NEVER_CANCELLED) -> ImportPlan:
    """Plan the import of ``analysis`` into ``dest_root`` for the chosen days."""
    dest_root = Path(os.path.abspath(dest_root))    # paths are measured and written absolute
    groups = group(analysis, day_start)
    with _ledger(ledger) as led:
        notes = _history_warnings(led)
        days = led.days_by_event(root_key(dest_root), day_start) if led is not None else {}
        existing = find_existing_events(dest_root, days)
        targets, warnings = resolve_targets(choices, dest_root, existing, merge_all=merge_all)
        index = DestIndex(dest_root, led)
        loose: list[tuple[SourceFile, Capture, Action]] = []
        for f in analysis.unknown_files:
            loose.append((f, metadata.capture_from_mtime(f.mtime_ns), Action.UNKNOWN))
        for f in analysis.orphans:
            loose.append((f, metadata.capture_from_mtime(f.mtime_ns), Action.ORPHAN))
        return build_plan(
            sources=analysis.sources, shots=analysis.shots, groups=groups, loose=loose,
            targets=targets, dest_root=dest_root, day_start=day_start, index=index,
            ledger=led, cfg=cfg, opts=opts, unknown=analysis.unknown, ignored=analysis.ignored,
            free_bytes=disk_free(dest_root) if dest_root.is_dir() else None,
            probe_write=probe_write,
            extra_warnings=(volumes.destination_notes(dest_root) + notes + warnings
                            + analysis.warnings),
            cancel=cancel)


# ---- execution ------------------------------------------------------------------------------


@contextlib.contextmanager
def import_lock() -> Iterator[None]:
    """Only one import or delete at a time (across processes). Raises AnotherImportRunning."""
    with file_lock(appdirs.lock_file()):
        yield


def execute(plan: ImportPlan, *, cancel: CancelToken = NEVER_CANCELLED, emit: Emit = no_emit,
            ops: FileOps | None = None, ledger: Ledger | None = None) -> ImportReport:
    """Copy and verify everything ``plan`` says. Writes a session log."""
    with import_lock(), _ledger(ledger) as led:
        if led is None:
            led = Ledger.memory()      # still copy safely; nothing is remembered
        report = _execute(plan, ledger=led, ops=ops, cancel=cancel, emit=emit)
    report.log_path = write_session_log(report)
    log.info("import %s finished: %s", plan.session_id, report.status)
    return report


def plan_deletion(report: ImportReport) -> DeletionPlan:
    return _plan_deletion(report)


def delete_originals(dplan: DeletionPlan, *, confirmed: bool,
                     cancel: CancelToken = NEVER_CANCELLED, emit: Emit = no_emit,
                     ops: FileOps | None = None, ledger: Ledger | None = None,
                     log_path: Path | None = None) -> DeletionReport:
    """Delete verified originals from the cards (``confirmed`` must be True)."""
    if not confirmed:
        raise ValueError("delete_originals needs confirmed=True")
    with import_lock(), _ledger(ledger) as led:
        dreport = _delete_originals(dplan, confirmed=True, ledger=led, ops=ops, cancel=cancel,
                                    emit=emit)
    append_deletion_log(log_path, dreport)
    return dreport


def default_choices(groups: Sequence[DayGroup], *, include: bool = True) -> list[DayChoice]:
    return [DayChoice(day=g.day, include=include) for g in groups]


def days_of(groups: Sequence[DayGroup]) -> list[date]:
    return [g.day for g in groups]
