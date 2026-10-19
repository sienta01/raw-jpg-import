"""Build the import plan: what to copy where, what to skip and why.

One function serves the GUI's live preview, the CLI's ``--dry-run`` and the real import,
so the preview cannot drift from what actually happens. Planning only stats files and
reads small bits of destination metadata; it never hashes and never writes (except an
optional write probe of the destination root).
"""

from __future__ import annotations

import os
import re
import stat
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path, PurePosixPath

from . import volumes
from .config import Config
from .destindex import MTIME_TOLERANCE_NS, DestFile, DestIndex
from .events import NEVER_CANCELLED, CancelToken
from .fsops import is_online_only, is_within
from .grouping import DayGroup, EventTarget, day_key
from .ledger import Ledger, Row, root_key
from .models import Capture, Source, SourceFile
from .naming import format_date, key
from .shots import Member, Shot, same_shot

MIN_HEADROOM = 512 * 1024 * 1024
WINDOWS_MAX_PATH = 259
MAX_RENAMES = 999

_VARIANT_TAIL = re.compile(r"-v\d+$", re.IGNORECASE)


class Action(Enum):
    COPY = "copy"
    SKIP_IMPORTED = "already imported"
    SKIP_EXISTS = "already in folder"
    SKIP_CULLED = "removed from library"
    SKIP_DUPLICATE = "duplicate"
    SKIP_DESELECTED = "day not selected"
    UNKNOWN = "unknown type"
    ORPHAN = "orphan sidecar"


@dataclass
class PlanItem:
    id: int
    src: SourceFile
    capture: Capture | None
    shot_id: int | None
    target: EventTarget | None
    dest_rel: PurePosixPath | None       # relative to target.path, e.g. RAW/DSC_0001-2.NEF
    action: Action
    reason: str = ""
    dup_of: int | None = None            # item id of the main member this duplicates
    renamed: bool = False
    existing: Path | None = None         # library file for SKIP_IMPORTED / SKIP_EXISTS
    ledger_id: int | None = None
    sha256: str | None = None            # hash recorded in the ledger, if any
    stem_used: str = ""                  # destination stem, e.g. "DSC_0001-2"
    variant: int = 0                     # 2, 3, ... for a second version of the same shot
    day: date | None = None              # the import day (day row) the item is planned under

    @property
    def dest(self) -> Path | None:
        if self.target is None or self.dest_rel is None:
            return None
        return self.target.path / Path(*self.dest_rel.parts)


@dataclass
class TargetSummary:
    target: EventTarget
    per_folder: dict[str, tuple[int, int]]   # folder -> (files to copy, bytes)
    renamed: int = 0
    skipped: int = 0

    @property
    def files(self) -> int:
        return sum(n for n, _ in self.per_folder.values())

    @property
    def bytes(self) -> int:
        return sum(b for _, b in self.per_folder.values())


@dataclass
class PlanOptions:
    reimport_culled: bool = False
    copy_unknown: bool | None = None     # None = use the config's [import] unknown policy


@dataclass
class ImportPlan:
    session_id: str
    created: datetime
    dest_root: Path
    day_start: time
    items: list[PlanItem]
    targets: list[TargetSummary]
    sources: list[Source]
    unknown: dict[str, int] = field(default_factory=dict)
    ignored: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)     # blocking
    bytes_to_copy: int = 0
    free_bytes: int = 0

    def counts(self) -> Counter[Action]:
        return Counter(i.action for i in self.items)

    @property
    def to_copy(self) -> list[PlanItem]:
        return [i for i in self.items if i.action is Action.COPY]

    @property
    def renamed(self) -> int:
        return sum(1 for i in self.items if i.action is Action.COPY and i.renamed)

    @property
    def blocked(self) -> bool:
        return bool(self.problems)

    def to_json(self) -> dict:
        counts = self.counts()
        return {
            "session": self.session_id,
            "dest_root": str(self.dest_root),
            "day_start": self.day_start.strftime("%H:%M"),
            "counts": {a.name.lower(): counts.get(a, 0) for a in Action},
            "renamed": self.renamed,
            "bytes_to_copy": self.bytes_to_copy,
            "free_bytes": self.free_bytes,
            "targets": [
                {"folder": t.target.folder, "path": str(t.target.path),
                 "exists": t.target.exists,
                 "days": sorted(format_date(d) for d in t.target.days),
                 "per_folder": {f: {"files": n, "bytes": b} for f, (n, b) in t.per_folder.items()},
                 "renamed": t.renamed, "skipped": t.skipped}
                for t in self.targets
            ],
            "items": [
                {"id": i.id, "action": i.action.name.lower(), "source": str(i.src.path),
                 "dest": str(i.dest) if i.dest else None, "reason": i.reason,
                 "renamed": i.renamed, "size": i.src.size,
                 "taken": i.capture.local.isoformat(sep=" ") if i.capture else None}
                for i in self.items
            ],
            "unknown": dict(self.unknown),
            "warnings": list(self.warnings),
            "problems": list(self.problems),
        }


# ---- helpers --------------------------------------------------------------------------------


def _target_key(t: EventTarget) -> str:
    return key(t.folder)


def _stem_from_dest_name(name: str, m: Member, shot_stem_key: str) -> str | None:
    """Recover the destination stem from a library file name for member ``m``.

    A ``-vN`` ending is dropped when what is left is the shot's stem (``DSC_0001``) or a
    renamed one (``DSC_0001-2``); a main member may carry one too when it was imported after
    another version of its shot.
    """
    suffix = m.suffix
    if not key(name).endswith(key(suffix)):
        return None
    stem = name[: len(name) - len(suffix)] if suffix else name
    v = _VARIANT_TAIL.search(stem)
    if v is not None and _is_stem_of(stem[: v.start()], shot_stem_key):
        stem = stem[: v.start()]
    elif m.variant_no:
        return None
    return stem or None


def _is_stem_of(stem: str, shot_stem_key: str) -> bool:
    k = key(stem)
    if k == shot_stem_key:
        return True
    head, dash, n = k.rpartition("-")
    return bool(dash) and head == shot_stem_key and n.isdigit()


def _check_ledger(ledger: Ledger | None, dest_root: Path, f: SourceFile,
                  cap: Capture | None, index: DestIndex | None = None,
                  ) -> tuple[str, Row | None, Path | None]:
    """Ledger verdict for one card file: ("imported" | "culled" | "none", row, library path).

    "imported" needs the recorded library file to still be that file: a file that took its
    name after the user removed it (same name, other size or capture) means "culled".
    """
    if ledger is None:
        return "none", None, None
    rk = root_key(dest_root)
    culled: Row | None = None
    for row in ledger.find(key(f.name), f.size, cap.local if cap else None):
        if row.dest_root != rk:
            continue
        event_dir = dest_root / row.event
        if not event_dir.is_dir():
            continue    # event folder renamed or moved: fall back to the disk checks
        lib = event_dir / Path(*PurePosixPath(row.dest_rel).parts)
        if _is_recorded_file(lib, row, index):
            return "imported", row, lib
        culled = culled or row
    if culled is not None:
        return "culled", culled, None
    return "none", None, None


def _is_recorded_file(lib: Path, row: Row, index: DestIndex | None) -> bool:
    """Whether ``lib`` is (still) the photo ``row`` recorded, not another one that took its
    name after the user removed it.

    Same size and file time: yes. Otherwise the exact capture times decide (a copy edited
    in place keeps its capture time), and when they cannot (no exact capture, an online-only
    copy), the size does.
    """
    try:
        st = lib.stat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    same_size = st.st_size == row.size
    if same_size and abs(st.st_mtime_ns - row.mtime_ns) <= MTIME_TOLERANCE_NS:
        return True
    recorded = _row_capture(row)
    if recorded is not None and recorded.exact and index is not None \
            and not is_online_only(st):
        own = index.own_capture(lib)
        if own is not None and own.exact:
            return same_shot(own, recorded)
    return same_size


def _row_capture(row: Row) -> Capture | None:
    if not row.capture:
        return None
    try:
        local = datetime.fromisoformat(row.capture)
    except ValueError:
        return None
    return Capture(local=local, source="ledger", exact=bool(row.capture_exact),
                   subsec_ms=row.subsec)


class _Planner:
    def __init__(self, dest_root: Path, index: DestIndex, ledger: Ledger | None,
                 cfg: Config, opts: PlanOptions, cancel: CancelToken) -> None:
        self.dest_root = dest_root
        self.index = index
        self.ledger = ledger
        self.cfg = cfg
        self.opts = opts
        self.cancel = cancel
        self.items: list[PlanItem] = []
        self.warnings: list[str] = []
        # target key -> stem key -> shot id that claimed it
        self.claimed: dict[str, dict[str, int]] = {}
        # (target key, folder key) -> names claimed by non-shot files (OTHER/)
        self.claimed_names: dict[tuple[str, str], set[str]] = {}
        self.item_of: dict[Path, int] = {}
        self.cross_folder_imported: Counter[str] = Counter()
        self.stem_mismatch = 0

    def add(self, **kw) -> PlanItem:
        item = PlanItem(id=len(self.items), **kw)
        self.items.append(item)
        self.item_of[item.src.path] = item.id
        return item

    # -- shots ------------------------------------------------------------------------------

    def plan_shot(self, shot: Shot, target: EventTarget | None) -> None:
        if target is None:
            for m in shot.members:
                self.add(src=m.file, capture=m.capture, shot_id=shot.id, target=None,
                         dest_rel=None, action=Action.SKIP_DESELECTED,
                         reason="day not selected")
            return

        pending: list[int] = []                         # indexes into shot.members
        verdicts: dict[int, tuple[Action, dict]] = {}
        forced_stem: str | None = None
        imported_paths: set[Path] = set()

        for i, m in enumerate(shot.members):
            if m.role == "duplicate":
                continue
            verdict, row, lib = _check_ledger(self.ledger, self.dest_root, m.file, m.capture,
                                              self.index)
            if verdict == "imported":
                assert row is not None and lib is not None
                stem = _stem_from_dest_name(lib.name, m, shot.stem_key)
                same_event = key(row.event) == key(target.folder)
                if stem and forced_stem is None and same_event:
                    forced_stem = stem
                if not same_event:
                    self.cross_folder_imported[row.event] += 1
                imported_paths.add(lib)
                rel = lib.relative_to(self.dest_root / row.event)
                verdicts[i] = (Action.SKIP_IMPORTED, dict(
                    existing=lib, ledger_id=row.id, sha256=row.sha256,
                    reason=f"already imported into {row.event}",
                    stem_used=stem or "", dest_rel=PurePosixPath(rel.as_posix()),
                    target_override=None if same_event else row.event))
            elif verdict == "culled" and not self.opts.reimport_culled:
                assert row is not None
                when = (row.at or "")[:10]
                verdicts[i] = (Action.SKIP_CULLED, dict(
                    ledger_id=row.id,
                    reason=f"imported {when} into {row.event}, removed from library since"))
            else:
                pending.append(i)

        stem_used = forced_stem or ""
        if pending:
            stem_used, results = self.allocate(shot, target, pending, forced_stem,
                                               imported_paths)
            verdicts.update(results)

        # Items in member order (duplicates last) so ids are deterministic.
        for i, m in enumerate(shot.members):
            if m.role == "duplicate":
                continue
            action, extra = verdicts[i]
            tgt = target
            override = extra.pop("target_override", None)
            if override is not None:
                tgt = EventTarget(path=self.dest_root / override, folder=override, exists=True,
                                  days=frozenset())
            self.add(src=m.file, capture=m.capture, shot_id=shot.id, target=tgt,
                     dest_rel=extra.pop("dest_rel", None), action=action,
                     stem_used=extra.pop("stem_used", stem_used), **extra)
        for m in shot.members:
            if m.role != "duplicate":
                continue
            main_id = self.item_of.get(m.dup_of.path) if m.dup_of is not None else None
            main = self.items[main_id] if main_id is not None else None
            self.add(src=m.file, capture=m.capture, shot_id=shot.id,
                     target=main.target if main else target,
                     dest_rel=main.dest_rel if main else None, action=Action.SKIP_DUPLICATE,
                     reason=f"identical to {m.dup_of.source.label}: {m.dup_of.rel}"
                     if m.dup_of else "duplicate", dup_of=main_id,
                     stem_used=main.stem_used if main else stem_used)

    def allocate(self, shot: Shot, target: EventTarget, pending: list[int],
                 forced: str | None, imported_paths: set[Path],
                 ) -> tuple[str, dict[int, tuple[Action, dict]]]:
        """Pick a destination stem for the pending members of ``shot`` in ``target``.

        A candidate stem is free when no other shot in this batch claimed it, no file of
        this shot would land on a different existing file, and every library file already
        using the stem (in any subfolder) belongs to this same shot.
        """
        claims = self.claimed.setdefault(_target_key(target), {})
        candidates: list[str] = [forced] if forced else []
        candidates.extend(self._partner_stems(shot, target))
        candidates.append(shot.stem)
        candidates.extend(f"{shot.stem}-{n}" for n in range(2, MAX_RENAMES + 1))
        seen: set[str] = set()
        for cand in candidates:
            ck = key(cand)
            if ck in seen:
                continue
            seen.add(ck)
            owner = claims.get(ck)
            if owner is not None and owner != shot.id:
                continue
            hits: dict[int, DestFile] = {}
            moved: dict[int, tuple[str, int]] = {}      # member -> (-vN name, N)
            planned = {(key(shot.members[i].folder), key(shot.members[i].dest_name(cand)))
                       for i in pending}
            ok = True
            for i in pending:
                m = shot.members[i]
                hit = self.index.listing(target, m.folder).get(key(m.dest_name(cand)))
                if hit is None:
                    continue
                if self.index.same_file(hit, m.file, m.capture, target):
                    hits[i] = hit
                    continue
                slot = self._variant_slot(m, cand, target, hit, planned)
                if slot is None:
                    ok = False
                    break
                name, n, same = slot
                moved[i] = (name, n)
                if same is not None:
                    hits[i] = same
            if not ok:
                continue
            ours = {h.path for h in hits.values()} | imported_paths
            for path, cap in self.index.stem_owners(target, ck):
                if path in ours or not self._owns_stem(path, cap):
                    continue
                if cap is None or not same_shot(cap, shot.capture):
                    ok = False
                    break
            if not ok:
                continue
            claims[ck] = shot.id
            if forced and ck != key(forced):
                self.stem_mismatch += 1
            renamed = ck != key(shot.stem)
            results: dict[int, tuple[Action, dict]] = {}
            for i in pending:
                m = shot.members[i]
                name, variant = moved.get(i, (m.dest_name(cand), m.variant_no))
                rel = PurePosixPath(m.folder) / name
                if i in hits:
                    results[i] = (Action.SKIP_EXISTS, dict(
                        existing=hits[i].path, dest_rel=rel, stem_used=cand,
                        reason="identical file already in the event folder"))
                else:
                    results[i] = (Action.COPY, dict(
                        dest_rel=rel, renamed=renamed, stem_used=cand, variant=variant,
                        reason=f"renamed: {m.file.name} -> {name}"
                        if renamed or variant else ""))
            return cand, results
        raise RuntimeError(f"no free name for {shot.stem} in {target.folder}")

    def _variant_slot(self, m: Member, cand: str, target: EventTarget, hit: DestFile,
                      planned: set[tuple[str, str]]) -> tuple[str, int, DestFile | None] | None:
        """The ``-vN`` name for a photo whose name is held by another version of the same shot.

        JPEG + JPEG slot mode imported one card at a time: the second card's JPG has the
        same exact capture as the library's but another size. It becomes the shot's next
        free ``-vN`` (or the ``-vN`` copy already there) instead of renaming the whole shot.
        Returns ``(name, N, identical library file or None)``, or None for a real clash.
        """
        if m.kind != "primary" or not m.capture.exact:
            return None
        other = self.index.capture_of(hit, target)
        if other is None or not other.exact or not same_shot(other, m.capture):
            return None
        listing = self.index.listing(target, m.folder)
        for n in range(2, MAX_RENAMES + 1):
            name = f"{cand}-v{n}{m.suffix}"
            slot = (key(m.folder), key(name))
            if slot in planned:
                continue
            there = listing.get(key(name))
            if there is not None and not self.index.same_file(there, m.file, m.capture, target):
                continue
            planned.add(slot)
            return name, n, there
        return None

    def _partner_stems(self, shot: Shot, target: EventTarget) -> list[str]:
        """Renamed stems (``DSC_0001-2``) that library files of this same shot already use.

        They come first, so a card imported later gets the name its partner got even after
        the plain stem became free again (the shot that held it was removed).
        """
        out: list[str] = []
        for n, sk in self.index.numbered_stems(target, shot.stem_key):
            if any(self._owns_stem(path, cap) and cap is not None
                   and same_shot(cap, shot.capture)
                   for path, cap in self.index.stem_owners(target, sk)):
                out.append(f"{shot.stem}-{n}")
        return out

    def _owns_stem(self, path: Path, cap: Capture | None) -> bool:
        """Whether a library file using a stem can keep another shot from taking it.

        Photos and videos can, and so can any file the history recorded (it knows the shot).
        Other files, such as an editor's .xmp or a voice memo once the history is lost, only
        have a file time, which says nothing about the shot, so they never force a rename.
        """
        if cap is not None and cap.source == "ledger":
            return True
        cat = self.cfg.formats.classify(path.name)
        return cat is not None and cat.is_primary

    # -- unknown files and orphan sidecars --------------------------------------------------

    def plan_loose(self, f: SourceFile, cap: Capture, target: EventTarget | None,
                   action_if_reported: Action) -> None:
        copy = self.cfg.formats.unknown_policy == "copy"
        if self.opts.copy_unknown is not None:
            copy = self.opts.copy_unknown
        if not copy:
            self.add(src=f, capture=cap, shot_id=None, target=None, dest_rel=None,
                     action=action_if_reported,
                     reason="unknown file type" if action_if_reported is Action.UNKNOWN
                     else "sidecar without a matching photo or video")
            return
        if target is None:
            self.add(src=f, capture=cap, shot_id=None, target=None, dest_rel=None,
                     action=Action.SKIP_DESELECTED, reason="day not selected")
            return
        verdict, row, lib = _check_ledger(self.ledger, self.dest_root, f, cap, self.index)
        if verdict == "imported" and row is not None and lib is not None:
            self.add(src=f, capture=cap, shot_id=None, target=target,
                     dest_rel=PurePosixPath(lib.relative_to(self.dest_root / row.event)
                                            .as_posix()),
                     action=Action.SKIP_IMPORTED, existing=lib, ledger_id=row.id,
                     sha256=row.sha256, reason=f"already imported into {row.event}")
            return
        if verdict == "culled" and not self.opts.reimport_culled and row is not None:
            self.add(src=f, capture=cap, shot_id=None, target=target, dest_rel=None,
                     action=Action.SKIP_CULLED, ledger_id=row.id,
                     reason=f"imported into {row.event}, removed from library since")
            return
        folder = self.cfg.formats.other_folder
        listing = self.index.listing(target, folder)
        taken = self.claimed_names.setdefault((_target_key(target), key(folder)), set())
        stem, dot, ext = f.name.rpartition(".")
        if not dot or not stem:
            stem, ext = f.name, ""
        for n in range(1, MAX_RENAMES + 1):
            name = f.name if n == 1 else (f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}")
            nk = key(name)
            if nk in taken:
                continue
            hit = listing.get(nk)
            if hit is not None:
                if self.index.same_file(hit, f, cap, target):
                    taken.add(nk)
                    self.add(src=f, capture=cap, shot_id=None, target=target,
                             dest_rel=PurePosixPath(folder) / name, action=Action.SKIP_EXISTS,
                             existing=hit.path, reason="identical file already in the folder")
                    return
                continue
            taken.add(nk)
            self.add(src=f, capture=cap, shot_id=None, target=target,
                     dest_rel=PurePosixPath(folder) / name, action=Action.COPY, renamed=n > 1,
                     reason=f"renamed: {f.name} -> {name}" if n > 1 else "")
            return
        raise RuntimeError(f"no free name for {f.name}")


def _problems(plan: ImportPlan, dest_root: Path, probe_write: bool,
              free: int | None) -> list[str]:
    problems: list[str] = []
    if not dest_root.is_dir():
        problems.append(f"Destination folder does not exist: {dest_root}")
        return problems
    nested = False
    for src in plan.sources:
        if is_within(dest_root, src.root) or is_within(src.root, dest_root):
            nested = True
            problems.append(f"Source {src.root} and destination {dest_root} are inside each "
                            "other; choose a destination outside the card")
            continue
        card = _card_of(src)
        if card is not None and is_within(dest_root, card.path):
            nested = True
            problems.append(f"Destination {dest_root} is on the same card as the source "
                            f"{src.root} ({card.label or card.path}); choose a destination "
                            "outside the card")
    if probe_write and not nested:      # never write a probe file onto a card
        probe = dest_root / f".rji-write-test-{uuid.uuid4().hex[:8]}"
        try:
            with open(probe, "xb"):
                pass
            os.remove(probe)
        except OSError as e:
            problems.append(f"Cannot write to {dest_root} ({e.strerror or e})")
    elif not os.access(dest_root, os.W_OK):
        problems.append(f"Cannot write to {dest_root}")
    if free is not None and plan.bytes_to_copy and free - plan.bytes_to_copy < MIN_HEADROOM:
        problems.append(f"Not enough space: needs {_gb(plan.bytes_to_copy)}, "
                        f"only {_gb(free)} free on the destination drive")
    checked: set[Path] = set()
    for item in plan.to_copy:
        assert item.target is not None and item.dest_rel is not None
        for parent in (item.target.path, item.target.path / item.dest_rel.parts[0]):
            if parent in checked:
                continue
            checked.add(parent)
            if parent.exists() and not parent.is_dir():
                problems.append(f"{parent} exists but is a file, not a folder")
        if sys.platform == "win32":
            dest = item.dest
            assert dest is not None
            # Measured absolute: a relative destination is longer on disk than as typed.
            longest = os.path.abspath(dest.with_name(f".{dest.name}.rji-000000.part"))
            if len(longest) > WINDOWS_MAX_PATH:
                problems.append(f"Path too long for Windows ({len(longest)} characters): "
                                f"{dest}. Choose a shorter destination or event name.")
                break
    return problems


def _card_of(src: Source) -> volumes.Volume | None:
    """The card a folder source lies on (``F:\\DCIM`` -> ``F:\\``), if it is on one."""
    if src.kind == "card":
        return None             # the source is the card's root: the nesting check covers it
    vol = volumes.volume_for_path(src.root)
    return vol if vol is not None and vol.is_card else None


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def build_plan(*, sources: list[Source], shots: list[Shot], groups: list[DayGroup],
               loose: list[tuple[SourceFile, Capture, Action]],
               targets: dict[date, EventTarget], dest_root: Path, day_start: time,
               index: DestIndex, ledger: Ledger | None, cfg: Config,
               opts: PlanOptions | None = None, unknown: dict[str, int] | None = None,
               ignored: Counter[str] | None = None, free_bytes: int | None = None,
               probe_write: bool = False, extra_warnings: list[str] | None = None,
               cancel: CancelToken = NEVER_CANCELLED, now: datetime | None = None) -> ImportPlan:
    """Plan an import.

    ``loose`` are files that are not part of a shot: unknown types (Action.UNKNOWN) and orphan
    sidecars (Action.ORPHAN), with their (mtime) capture. ``free_bytes`` is the free space on
    the destination drive (``None`` skips the space check).
    """
    opts = opts or PlanOptions()
    dest_root = Path(os.path.abspath(dest_root))
    p = _Planner(dest_root, index, ledger, cfg, opts, cancel)
    shot_day: dict[int, date] = {}
    for g in groups:
        for s in g.shots:
            shot_day[s.id] = g.day
    for n, shot in enumerate(shots):
        if n % 200 == 0:
            cancel.check()
        day = shot_day.get(shot.id, day_key(shot.capture.local, day_start))
        first = len(p.items)
        p.plan_shot(shot, targets.get(day))
        for it in p.items[first:]:
            it.day = day
    # A loose file from a day without photos has no day row to tick, so it goes with the
    # nearest included day (it is only copied when unknown files are copied at all).
    row_days = set(shot_day.values())
    included = sorted(targets)
    rehomed: Counter[str] = Counter()
    for f, cap, action in loose:
        day = day_key(cap.local, day_start)
        moved = day not in row_days and day not in targets and bool(included)
        if moved:
            day = min(included, key=lambda d, own=day: (abs((d - own).days), d))
        p.plan_loose(f, cap, targets.get(day), action)
        p.items[-1].day = day
        if moved and p.items[-1].action is Action.COPY:
            rehomed[targets[day].folder] += 1
    for folder, count in sorted(rehomed.items()):
        p.warnings.append(f"{count} files are from days without photos and go into {folder}")

    plan = ImportPlan(
        session_id=(now or datetime.now()).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        created=now or datetime.now(), dest_root=dest_root, day_start=day_start,
        items=p.items, targets=[], sources=list(sources), unknown=dict(unknown or {}),
        ignored=Counter(ignored or {}), warnings=list(extra_warnings or []),
    )
    summaries: dict[str, TargetSummary] = {}
    for t in {_target_key(t): t for t in targets.values()}.values():
        summaries[_target_key(t)] = TargetSummary(target=t, per_folder={})
    for item in plan.items:
        if item.target is None:
            continue
        s = summaries.get(_target_key(item.target))
        if s is None:
            continue
        if item.action is Action.COPY:
            assert item.dest_rel is not None
            folder = item.dest_rel.parts[0]
            n, b = s.per_folder.get(folder, (0, 0))
            s.per_folder[folder] = (n + 1, b + item.src.size)
            s.renamed += int(item.renamed)
        else:
            s.skipped += 1
    order = {f: n for n, f in enumerate(cfg.formats.folders() + [cfg.formats.other_folder])}
    for s in summaries.values():
        s.per_folder = dict(sorted(s.per_folder.items(),
                                   key=lambda kv: (order.get(kv[0], len(order)), kv[0])))
    plan.targets = sorted(summaries.values(), key=lambda s: (min(s.target.days, default=date.max),
                                                             s.target.folder))
    plan.bytes_to_copy = sum(i.src.size for i in plan.to_copy)
    plan.free_bytes = free_bytes or 0

    _add_warnings(plan, p)
    plan.problems = _problems(plan, dest_root, probe_write, free_bytes)
    if not targets:
        plan.problems.append("No days selected")
    return plan


def _add_warnings(plan: ImportPlan, p: _Planner) -> None:
    counts = plan.counts()
    # Files dated by their partner (sidecars, a JPG without EXIF next to its NEF) have a date.
    fallback = sum(1 for i in plan.items if i.action is Action.COPY and i.capture
                   and not i.capture.exact and i.capture.source != "partner")
    if fallback:
        plan.warnings.append(f"{fallback} files have no capture date; using the file time")
    suspicious = sum(1 for i in plan.items if i.action is Action.COPY and i.capture
                     and i.capture.suspicious)
    if suspicious:
        plan.warnings.append(f"{suspicious} files have a suspicious date (camera clock not set?)")
    if plan.unknown:
        kinds = ", ".join(f".{e or '(none)'} x{n}" for e, n in sorted(plan.unknown.items()))
        plan.warnings.append(f"Files of unknown type: {kinds}")
    if counts.get(Action.SKIP_DUPLICATE):
        plan.warnings.append(f"{counts[Action.SKIP_DUPLICATE]} files are identical on both cards "
                             "(backup mode) and will be copied once")
    variants = sum(1 for i in plan.items if i.action is Action.COPY and i.variant)
    if variants:
        plan.warnings.append(f"{variants} files are a second version of the same shot "
                             "and get a -v2 suffix")
    for event, n in sorted(p.cross_folder_imported.items()):
        plan.warnings.append(f"{n} files were already imported into {event}")
    if counts.get(Action.SKIP_CULLED):
        plan.warnings.append(f"{counts[Action.SKIP_CULLED]} previously imported files were "
                             "removed from the library and will not be imported again")
    if p.stem_mismatch:
        plan.warnings.append(f"{p.stem_mismatch} photos could not reuse the name of their "
                             "already-imported partner file")
    plan.warnings.extend(p.warnings)
