"""UI state and rules for the window, kept free of tkinter so they can be unit-tested.

The Tk widgets only mirror what this module says: which sources and days are shown, what
each event-name box holds, which rows share a folder, what the Import button reads and
whether it is enabled. Workers get plain snapshots (:class:`PlanRequest`), never the live
model and never tk Variables.
"""

from __future__ import annotations

import enum
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .. import engine
from ..config import Config
from ..copier import ImportReport, ItemResult, Outcome
from ..deleter import DeletionPlan
from ..engine import Analysis, ScannedSource
from ..events import NEVER_CANCELLED, CancelToken, Progress
from ..grouping import DayChoice, DayGroup, ExistingEvent, day_key, suggestions_for
from ..models import Capture, Source
from ..naming import key, sanitize_component
from ..planner import Action, ImportPlan, PlanItem, PlanOptions
from ..report import fmt_bytes, plural
from ..shots import Shot
from ..volumes import Volume

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAY_STARTS = tuple(f"{h:02d}:00" for h in range(7))
DOT = " · "
WARN = "⚠"
NO_DAYS = "No days selected"          # planner problem shown as a hint, not in red


class AppState(enum.Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    IMPORTING = "importing"
    DELETING = "deleting"


# ---- formatting -----------------------------------------------------------------------------


def fmt_day(d: date) -> str:
    """``2026.01.23 Fri`` (English weekday names whatever the locale)."""
    return f"{d.year:04d}.{d.month:02d}.{d.day:02d} {WEEKDAYS[d.weekday()]}"


def fmt_short_day(d: date) -> str:
    """``Fri 23 Jan``."""
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def fmt_time_range(first: datetime, last: datetime) -> str:
    return f"{first:%H:%M}-{last:%H:%M}"


def fmt_eta(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{max(1, round(s / 60))} min"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def fmt_taken(cap: Capture | None) -> str:
    if cap is None:
        return ""
    text = f"{cap.local:%Y-%m-%d %H:%M:%S}"
    return text if cap.exact else text + " (file time)"


def fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def parse_hhmm(text: str, default: time = time(0)) -> time:
    try:
        return engine.parse_day_start(text)
    except ValueError:
        return default


def progress_fraction(ev: Progress) -> float:
    if ev.bytes_total:
        frac = ev.bytes_done / ev.bytes_total
    elif ev.files_total:
        frac = ev.files_done / ev.files_total
    else:
        return 0.0
    return min(max(frac, 0.0), 1.0)


def progress_text(ev: Progress) -> str:
    """``212 / 455 files · 28.1 GB / 61.2 GB · 94.0 MB/s · about 6 min left``."""
    if ev.phase == "scan":
        return f"Scanning... {plural(ev.files_done, 'file')} found"
    if ev.phase == "metadata":
        return f"Reading dates {ev.files_done}/{ev.files_total}"
    if ev.phase == "delete":
        return f"{ev.files_done} / {plural(ev.files_total, 'file')} checked"
    parts = [f"{ev.files_done} / {plural(ev.files_total, 'file')}"]
    if ev.bytes_total:
        parts.append(f"{fmt_bytes(ev.bytes_done)} / {fmt_bytes(ev.bytes_total)}")
    if ev.rate_bps:
        parts.append(f"{fmt_bytes(ev.rate_bps)}/s")
    if ev.eta_s is not None:
        parts.append(f"about {fmt_eta(ev.eta_s)} left")
    return DOT.join(parts)


def scan_summary(scan: ScannedSource | None) -> str:
    """``1,204 files · 58.1 GB`` for a scanned source."""
    if scan is None:
        return ""
    files = scan.scan.files
    return f"{len(files):,} {'file' if len(files) == 1 else 'files'}{DOT}" \
           f"{fmt_bytes(scan.scan.total_bytes)}"


def short_path(path: Path | str, limit: int = 60) -> str:
    """``C:\\Users\\...\\cards\\SLOT1``: long paths lose their middle, never their end."""
    text = str(path)
    if len(text) <= limit:
        return text
    keep = limit - 3
    head = keep // 3
    return text[:head] + "..." + text[len(text) - (keep - head):]


def dest_path(text: str) -> Path | None:
    """The destination typed by the user as a Path (``None`` when empty)."""
    t = text.strip().strip('"').strip()
    if not t:
        return None
    if sys.platform == "win32" and re.fullmatch(r"[A-Za-z]:", t):
        t += "\\"              # "D:" alone means "the current folder on D:"
    return Path(t)


# ---- sources --------------------------------------------------------------------------------


@dataclass
class SourceEntry:
    """One row of the Sources list (a detected card or a folder the user added)."""

    source: Source
    checked: bool = True
    present: bool = True          # False once a card was removed from the reader
    status: str = "waiting"       # waiting | scanning | scanned | error
    detail: str = ""              # scan progress or error text

    @property
    def key(self) -> str:
        return self.source.key

    @property
    def is_card(self) -> bool:
        return self.source.kind == "card"

    @property
    def title(self) -> str:
        return self.source.label if self.is_card else f"{self.source.label} (folder)"

    def status_text(self, scan: ScannedSource | None) -> str:
        if not self.present:
            return "Card removed"
        if self.status == "scanning":
            return self.detail or "Scanning..."
        if self.status == "waiting":
            return "Waiting to scan" if self.checked else "Not scanned"
        if self.status == "error":
            return f"Could not scan: {self.detail}"
        errors = len(scan.scan.errors) if scan is not None else 0
        return "✓ Scanned" + (f" ({errors} could not be read)" if errors else "")


@dataclass
class CardChanges:
    added: list[SourceEntry] = field(default_factory=list)
    returned: list[SourceEntry] = field(default_factory=list)
    removed: list[SourceEntry] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.returned or self.removed)


# ---- days, names and joins ------------------------------------------------------------------


@dataclass(frozen=True)
class ChoiceState:
    """A frozen copy of what the user chose for each day (safe to hand to a worker)."""

    names: Mapping[date, str]
    joins: Mapping[date, bool]
    checked: Mapping[date, bool]
    merge_all: bool = False


def prefill_name(day: date, suggestions: Sequence[ExistingEvent]) -> str:
    """The name shown for a day the user has not typed into.

    One existing folder holds the day: its name part when the folder is dated that day
    (so the folder is reused), else its full folder name. Several: empty, the dropdown
    lists them. None: empty.
    """
    if len(suggestions) == 1:
        e = suggestions[0]
        return e.name if e.date == day else e.folder
    return ""


def name_for(day: date, existing: Sequence[ExistingEvent], names: Mapping[date, str]) -> str:
    if day in names:
        return names[day]
    return prefill_name(day, suggestions_for(day, existing))


def combo_values(day: date, existing: Sequence[ExistingEvent],
                 recent: Sequence[str]) -> list[str]:
    """Existing folders holding ``day`` (full names), then the recent event names."""
    out: list[str] = []
    seen: set[str] = set()
    for text in [e.folder for e in suggestions_for(day, existing)] + list(recent):
        k = key(text.strip())
        if text.strip() and k not in seen:
            seen.add(k)
            out.append(text)
    return out


def choices_for(groups: Sequence[DayGroup], existing: Sequence[ExistingEvent],
                state: ChoiceState) -> list[DayChoice]:
    """One DayChoice per day for ``engine.plan``.

    A join only counts when an earlier day is included; with ``merge_all`` every included
    day after the first one joins.
    """
    out: list[DayChoice] = []
    seen_included = False
    for g in groups:
        include = state.checked.get(g.day, True)
        wants = state.merge_all or state.joins.get(g.day, False)
        out.append(DayChoice(day=g.day, include=include,
                             text=name_for(g.day, existing, state.names),
                             join_previous=include and seen_included and wants))
        seen_included = seen_included or include
    return out


def new_counts(plan: ImportPlan, groups: Sequence[DayGroup], day_start: time,
               choices: Sequence[DayChoice]) -> dict[date, int]:
    """Files to copy per included day (excluded days are left out: unknown)."""
    shot_day = {s.id: g.day for g in groups for s in g.shots}
    out = {c.day: 0 for c in choices if c.include}
    for item in plan.items:
        if item.action is not Action.COPY:
            continue
        if item.shot_id is not None and item.shot_id in shot_day:
            d = shot_day[item.shot_id]
        elif item.capture is not None:
            d = day_key(item.capture.local, day_start)
        else:
            continue
        if d in out:
            out[d] += 1
    return out


def midnight_banner(shots: Sequence[Shot], day_start: time,
                    suggestion: time | None) -> tuple[str, str] | None:
    """``(message, button label)`` when shooting ran past midnight, else None."""
    if suggestion is None or suggestion <= day_start:
        return None
    moved = sorted(s.capture.local for s in shots
                   if day_start <= s.capture.local.time() < suggestion)
    if not moved:
        return None
    nights = sorted({t.date() for t in moved})
    first = [t for t in moved if t.date() == nights[0]]
    where = f"{fmt_time_range(first[0], first[-1])} on {fmt_short_day(nights[0])}"
    more = f" and {plural(len(nights) - 1, 'other night')}" if len(nights) > 1 else ""
    msg = (f"Shooting continued past midnight ({where}{more}). Starting days at "
           f"{fmt_hhmm(suggestion)} keeps it with the evening before.")
    if len(nights) == 1:
        button = f"Count as {fmt_short_day(nights[0] - timedelta(days=1))}"
    else:
        button = f"Start days at {fmt_hhmm(suggestion)}"
    return msg, button


@dataclass(frozen=True)
class DayRow:
    day: date
    label: str                    # 2026.01.23 Fri
    shot: str                     # 17:42-23:58
    counts: dict[str, int]
    new: int | None               # None = not known yet
    checked: bool
    join: bool                    # shown as "same folder as above"
    join_enabled: bool
    name: str
    values: list[str]
    fallback: int                 # shots without an exact capture time


@dataclass(frozen=True)
class DestRow:
    status: str                   # NEW | EXISTING
    folder: str                   # with a warning mark when sanitising changed the name
    contents: str
    warning: str = ""


@dataclass(frozen=True)
class NoticeRow:
    level: str                    # info | warning
    text: str
    action: str = ""              # "unknown" | "errors": a [Details...] button


@dataclass(frozen=True)
class Enabled:
    import_: bool
    preview: bool
    edit: bool                    # sources, destination, days
    settings: bool
    why_not: str = ""             # why Import is disabled (status line)


def enabled_actions(state: AppState, *, scanning: bool, dest_set: bool,
                    plan: ImportPlan | None, plan_pending: bool,
                    blocked: str | None = None, any_checked: bool = True,
                    checking: bool = False) -> Enabled:
    busy = state in (AppState.IMPORTING, AppState.DELETING)
    if busy:
        return Enabled(False, False, False, False, "")
    if checking:                  # the final plan is being built from the current choices
        return Enabled(False, False, False, False, "Checking the destination...")
    why = ""
    if blocked:
        why = blocked
    elif scanning or state is AppState.SCANNING:
        why = "Scanning the cards..."
    elif not dest_set:
        why = "Choose a destination folder"
    elif not any_checked:
        why = "Tick at least one day"
    elif plan is None or plan_pending:
        why = "Updating..."
    elif plan.problems:
        why = "Fix the problems shown above"
    elif not plan.to_copy:
        why = "Nothing new to import"
    preview = plan is not None and not plan_pending
    return Enabled(import_=not why, preview=preview, edit=True, settings=True, why_not=why)


# ---- live plan (worker side) ----------------------------------------------------------------


@dataclass(frozen=True)
class PlanRequest:
    analysis: Analysis | None
    dest_root: Path | None
    day_start: time
    cfg: Config
    state: ChoiceState
    opts: PlanOptions
    probe_write: bool = False
    context: int = 0              # SetupModel.context_gen when the request was made


@dataclass
class PlanResult:
    plan: ImportPlan | None
    existing: list[ExistingEvent]
    free_bytes: int | None
    new_by_day: dict[date, int]
    choices: list[DayChoice]
    dest_ok: bool = True
    context: int | None = None    # copied from the request (None: unknown, accepted)


def compute_plan(req: PlanRequest, *, cancel: CancelToken = NEVER_CANCELLED) -> PlanResult:
    """Runs on a worker thread: existing folders, free space and the plan itself."""
    result = _compute_plan(req, cancel)
    result.context = req.context
    return result


def _compute_plan(req: PlanRequest, cancel: CancelToken) -> PlanResult:
    dest = req.dest_root
    if dest is None:
        return PlanResult(None, [], None, {}, [], dest_ok=False)
    if not dest.is_dir():
        return PlanResult(None, [], None, {}, [], dest_ok=False)
    existing = engine.existing_events(dest, day_start=req.day_start)
    free = engine.disk_free(dest)
    if req.analysis is None:
        return PlanResult(None, existing, free, {}, [])
    groups = engine.group(req.analysis, req.day_start)
    if not groups:
        return PlanResult(None, existing, free, {}, [])
    choices = choices_for(groups, existing, req.state)
    plan = engine.plan(req.analysis, choices, dest_root=dest, day_start=req.day_start,
                       cfg=req.cfg, opts=req.opts, probe_write=req.probe_write, cancel=cancel)
    return PlanResult(plan, existing, free, new_counts(plan, groups, req.day_start, choices),
                      choices)


# ---- the setup page model -------------------------------------------------------------------


class SetupModel:
    """Everything the setup page shows, and the rules that connect it."""

    def __init__(self, *, day_start: time = time(0), hide_imported: bool = True,
                 recent_names: Sequence[str] = (), dest_text: str = "") -> None:
        self.sources: list[SourceEntry] = []
        self.scans: dict[str, ScannedSource] = {}
        self._order = 0
        self.dest_text = dest_text
        self.day_start = day_start
        self.hide_imported = hide_imported
        self.recent_names = list(recent_names)
        self.folder_order: list[str] = []
        self.config_error: str | None = None
        # per-day choices; typed names survive regrouping and grid rebuilds
        self.names_by_date: dict[date, str] = {}
        self.joins: dict[date, bool] = {}
        self.saved_joins: dict[date, bool] | None = None
        self.merge_all = False
        self.checked: dict[date, bool] = {}
        self.hidden: set[date] = set()
        self.pending_auto_hide = True
        self.context_gen = 0          # bumped whenever sources, destination or day start change
        self.checking = False         # the final plan for Import is being built
        self.reimport_culled = False
        self.culled_seen = 0
        # derived from scans and the live plan
        self.analysis: Analysis | None = None
        self.groups: list[DayGroup] = []
        self.existing: list[ExistingEvent] = []
        self.free_bytes: int | None = None
        self.dest_ok = False
        self.new_by_day: dict[date, int] = {}
        self.plan: ImportPlan | None = None
        self.plan_error: str | None = None
        self.plan_pending = False
        self.last_choices: list[DayChoice] = []

    # -- sources ----------------------------------------------------------------------------

    def next_order(self) -> int:
        self._order += 1
        return self._order

    def find(self, source_key: str) -> SourceEntry | None:
        for e in self.sources:
            if e.key == source_key:
                return e
        return None

    def add_folder(self, path: Path | str) -> SourceEntry:
        """A manual folder source (an existing entry for the same folder is reused)."""
        src = engine.make_source(Path(path), order=self.next_order())
        old = self.find(src.key)
        if old is not None:
            old.checked = True
            return old
        entry = SourceEntry(src)
        self.sources.append(entry)
        return entry

    def remove_source(self, source_key: str) -> SourceEntry | None:
        entry = self.find(source_key)
        if entry is not None:
            self.sources.remove(entry)
            self.scans.pop(source_key, None)
        return entry

    def set_source_checked(self, source_key: str, on: bool) -> SourceEntry | None:
        entry = self.find(source_key)
        if entry is not None:
            entry.checked = on
        return entry

    def cards_changed(self, volumes: Sequence[Volume]) -> CardChanges:
        """Apply a card detection result: new cards are added checked, gone ones greyed."""
        now: dict[str, Volume] = {}
        for v in volumes:
            now[engine.make_source(v.path, volume=v).key] = v
        changes = CardChanges()
        for e in self.sources:
            if not e.is_card:
                continue
            if e.key in now:
                if not e.present:
                    e.present = True
                    e.status = "waiting"        # it may have changed in the camera: rescan
                    e.detail = ""
                    changes.returned.append(e)
            elif e.present:
                e.present = False
                changes.removed.append(e)
        for k, v in now.items():
            if self.find(k) is None:
                entry = SourceEntry(engine.make_source(v.path, order=self.next_order(),
                                                       volume=v))
                self.sources.append(entry)
                changes.added.append(entry)
        return changes

    def next_to_scan(self) -> SourceEntry | None:
        for e in self.sources:
            if e.checked and e.present and e.status == "waiting":
                return e
        return None

    @property
    def scanning(self) -> bool:
        return any(e.checked and e.present and e.status in ("waiting", "scanning")
                   for e in self.sources)

    def mark_for_rescan(self) -> None:
        for e in self.sources:
            if e.present and e.status != "scanning":
                e.status = "waiting"
                e.detail = ""

    def scan_finished(self, source_key: str, scanned: ScannedSource) -> SourceEntry | None:
        entry = self.find(source_key)
        if entry is None:
            return None
        self.scans[source_key] = scanned
        entry.status = "scanned"
        entry.detail = ""
        return entry

    def scan_failed(self, source_key: str, message: str) -> SourceEntry | None:
        entry = self.find(source_key)
        if entry is not None:
            entry.status = "error"
            entry.detail = message
        return entry

    def selected_scans(self) -> list[ScannedSource]:
        return [self.scans[e.key] for e in self.sources
                if e.checked and e.present and e.key in self.scans]

    # -- analysis and context ---------------------------------------------------------------

    def rebuild(self) -> None:
        """Re-analyse the selected scans and regroup them by day (no rescan)."""
        scans = self.selected_scans()
        self.analysis = engine.analyze(scans) if scans else None
        self.groups = engine.group(self.analysis, self.day_start) if self.analysis else []
        self.reset_context()

    def set_day_start(self, t: time) -> bool:
        if t == self.day_start:
            return False
        self.day_start = t
        self.rebuild()
        return True

    def set_dest(self, text: str) -> bool:
        if text == self.dest_text:
            return False
        self.dest_text = text
        self.reset_context()
        return True

    def dest_path(self) -> Path | None:
        return dest_path(self.dest_text)

    def reset_context(self) -> None:
        """Sources, destination or day start changed: New counts must be worked out again.

        Auto-hidden days are put back into the next plan (still hidden) so the first plan
        result can decide again which days have nothing new.
        """
        for d in self.hidden:
            self.checked[d] = True
        self.new_by_day.clear()
        self.pending_auto_hide = True
        self.context_gen += 1

    def reset_days(self) -> None:
        """Forget every per-day choice (New import)."""
        self.names_by_date.clear()
        self.joins.clear()
        self.saved_joins = None
        self.merge_all = False
        self.checked.clear()
        self.hidden.clear()
        self.reimport_culled = False
        self.culled_seen = 0
        self.plan = None
        self.reset_context()

    # -- per-day choices --------------------------------------------------------------------

    def is_checked(self, day: date) -> bool:
        return self.checked.get(day, True)

    def is_visible(self, day: date) -> bool:
        return not (self.hide_imported and day in self.hidden)

    def visible_days(self) -> list[date]:
        return [g.day for g in self.groups if self.is_visible(g.day)]

    def set_checked(self, day: date, on: bool) -> None:
        self.checked[day] = on
        if on:
            self.hidden.discard(day)

    def set_name(self, day: date, text: str) -> None:
        self.names_by_date[day] = text

    def name(self, day: date) -> str:
        return name_for(day, self.existing, self.names_by_date)

    def set_join(self, day: date, on: bool) -> None:
        if not self.merge_all:
            self.joins[day] = on

    def set_merge_all(self, on: bool) -> None:
        """One folder for all checked days; turning it off restores the per-row joins."""
        if on and not self.merge_all:
            self.saved_joins = dict(self.joins)
            self.merge_all = True
        elif not on and self.merge_all:
            self.joins = dict(self.saved_joins or {})
            self.saved_joins = None
            self.merge_all = False

    def set_hide_imported(self, on: bool) -> None:
        self.hide_imported = on
        if not on:
            self.hidden.clear()
            return
        for g in self.groups:
            if self.new_by_day.get(g.day) == 0:
                self.hidden.add(g.day)
                self.checked[g.day] = False

    def effective_join(self, day: date) -> bool:
        """Whether ``day`` goes into the folder of the checked day above it."""
        if not self.is_checked(day):
            return False
        earlier = False
        for g in self.groups:
            if g.day == day:
                break
            if self.is_checked(g.day):
                earlier = True
        if not earlier:
            return False
        return self.merge_all or self.joins.get(day, False)

    def snapshot(self) -> ChoiceState:
        return ChoiceState(names=dict(self.names_by_date), joins=dict(self.joins),
                           checked={g.day: self.is_checked(g.day) for g in self.groups},
                           merge_all=self.merge_all)

    def inputs(self) -> tuple[object, ...]:
        """Everything a plan is computed from; equal values mean the plan is still valid."""
        return (self.context_gen, self.snapshot(), self.reimport_culled)

    def columns(self) -> list[str]:
        """Count columns: the folders present in the scanned days, in config order."""
        present: set[str] = set()
        for g in self.groups:
            present.update(f for f, n in g.counts.items() if n)
        cols = [f for f in self.folder_order if f in present]
        return cols + sorted(present - set(cols))

    def rows(self) -> list[DayRow]:
        out: list[DayRow] = []
        first = True
        for g in self.groups:
            if not self.is_visible(g.day):
                continue
            out.append(DayRow(
                day=g.day, label=fmt_day(g.day), shot=fmt_time_range(g.first, g.last),
                counts=dict(g.counts), new=self.new_by_day.get(g.day),
                checked=self.is_checked(g.day),
                join=not first and self.effective_join(g.day),
                join_enabled=not first and not self.merge_all,
                name=self.name(g.day),
                values=combo_values(g.day, self.existing, self.recent_names),
                fallback=g.fallback_dates))
            first = False
        return out

    # -- plan -------------------------------------------------------------------------------

    def plan_request(self, cfg: Config, *, probe_write: bool = False) -> PlanRequest:
        return PlanRequest(analysis=self.analysis, dest_root=self.dest_path(),
                           day_start=self.day_start, cfg=cfg, state=self.snapshot(),
                           opts=PlanOptions(reimport_culled=self.reimport_culled),
                           probe_write=probe_write, context=self.context_gen)

    def is_current(self, result: PlanResult) -> bool:
        """False for a result computed before the sources, destination or day start changed."""
        return result.context is None or result.context == self.context_gen

    def apply_plan(self, result: PlanResult) -> bool:
        """Take a fresh plan. True when days were hidden or shown, so it must be re-planned.

        A result from an older context is ignored: it must not decide which days of the new
        destination count as already imported.
        """
        if not self.is_current(result):
            return False
        self.plan = result.plan
        self.plan_error = None
        self.existing = result.existing
        self.free_bytes = result.free_bytes
        self.dest_ok = result.dest_ok
        self.last_choices = result.choices
        self.new_by_day.update(result.new_by_day)
        if result.plan is not None and not self.reimport_culled:
            self.culled_seen = result.plan.counts().get(Action.SKIP_CULLED, 0)
        if not self.pending_auto_hide or result.plan is None:
            return False
        self.pending_auto_hide = False
        if not self.hide_imported:
            return False
        changed = False
        for g in self.groups:
            n = result.new_by_day.get(g.day)
            if n is None:
                continue                     # unticked by the user: nothing known
            if n == 0:
                self.hidden.add(g.day)
                if self.is_checked(g.day):
                    self.checked[g.day] = False
                    changed = True
            else:
                self.hidden.discard(g.day)
        return changed

    def plan_failed(self, message: str) -> None:
        self.plan = None
        self.plan_error = message

    @property
    def any_checked(self) -> bool:
        return any(self.is_checked(g.day) for g in self.groups)

    def enabled(self, state: AppState) -> Enabled:
        blocked = None
        if self.config_error:
            blocked = "Fix the config file first (see Settings)"
        return enabled_actions(state, scanning=self.scanning, dest_set=self.dest_path() is not None,
                               plan=self.plan, plan_pending=self.plan_pending, blocked=blocked,
                               any_checked=self.any_checked or not self.groups,
                               checking=self.checking)

    def import_label(self) -> str:
        if self.plan is None or not self.plan.to_copy:
            return "Import"
        return f"Import {plural(len(self.plan.to_copy), 'file')}"

    def space_line(self) -> str:
        if self.plan is None:
            return ""
        if not self.plan.to_copy:
            return "Nothing new to copy"
        need = fmt_bytes(self.plan.bytes_to_copy)
        if self.free_bytes:
            return f"Needs {need} of {fmt_bytes(self.free_bytes)}"
        return f"Needs {need}"

    def dest_info(self) -> str:
        dest = self.dest_path()
        if dest is None:
            return "Choose the folder that holds your event folders."
        if not self.dest_ok:
            return "" if self.plan_pending else "This folder does not exist."
        parts = []
        if self.free_bytes is not None:
            parts.append(f"{fmt_bytes(self.free_bytes)} free")
        parts.append(f"{plural(len(self.existing), 'event folder')} found")
        return DOT.join(parts)

    def hidden_status(self) -> str:
        if not self.hide_imported:
            return ""
        n = sum(1 for g in self.groups if g.day in self.hidden)
        if not n:
            return ""
        return f"{plural(n, 'day')} already imported (hidden)"

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.config_error:
            out.append(f"Config file error: {self.config_error}")
        if self.plan_error:
            out.append(f"Could not plan the import: {self.plan_error}")
        if self.plan is not None:
            out.extend(p for p in self.plan.problems if p != NO_DAYS)
        return out

    def notices(self) -> list[NoticeRow]:
        out: list[NoticeRow] = []
        a = self.analysis
        if self.plan is not None:
            plan = self.plan
            for w in plan.warnings:
                if w.startswith("Files of unknown type"):
                    n = sum(plan.unknown.values())
                    kinds = ", ".join(f".{(e or '(none)').upper()} ×{k}"
                                      for e, k in sorted(plan.unknown.items()))
                    copied = not plan.counts().get(Action.UNKNOWN)
                    verb = "go into the OTHER folder" if copied else "won't be imported"
                    out.append(NoticeRow("warning", f"{plural(n, 'file')} of unknown type "
                                                    f"{verb} ({kinds})", "unknown"))
                else:
                    out.append(NoticeRow("info", w))
        elif a is not None:
            out.extend(NoticeRow("info", w) for w in a.warnings)
            if a.unknown:
                n = sum(a.unknown.values())
                out.append(NoticeRow("warning", f"{plural(n, 'file')} of unknown type",
                                     "unknown"))
        if a is not None and a.errors:
            what = plural(len(a.errors), "file or folder", "files or folders")
            out.append(NoticeRow("warning", f"{what} could not be read", "errors"))
        return out

    def unknown_details(self) -> str:
        if self.analysis is None:
            return ""
        return "\n".join(f"{f.source.label}: {f.rel}" for f in self.analysis.unknown_files)

    def error_details(self) -> str:
        if self.analysis is None:
            return ""
        return "\n".join(f"{p}: {err}" for p, err in self.analysis.errors)

    def destination_rows(self) -> list[DestRow]:
        if self.plan is None:
            return []
        text_of = {c.day: c.text for c in self.last_choices if c.include}
        rows: list[DestRow] = []
        for t in self.plan.targets:
            parts = [f"{folder} {n}" for folder, (n, _) in t.per_folder.items()]
            contents = DOT.join(parts) or "nothing new"
            if t.renamed:
                contents += f" ({t.renamed} renamed)"
            warning = ""
            if not t.target.exists and t.target.days:
                warning = "; ".join(_sanitize_reasons(text_of.get(min(t.target.days), "")))
            folder = t.target.folder + (f"   {WARN} {warning}" if warning else "")
            rows.append(DestRow("EXISTING" if t.target.exists else "NEW", folder, contents,
                                warning))
        return rows

    def confirm_text(self, plan: ImportPlan) -> str:
        return confirm_text(plan)

    def names_to_remember(self) -> list[str]:
        """Names the user typed for days that start a folder in the last plan."""
        out: list[str] = []
        for c in self.last_choices:
            if not c.include or c.join_previous:
                continue
            text = self.names_by_date.get(c.day, "").strip()
            if text and text not in out:
                out.append(text)
        return out

    def banner(self) -> tuple[str, str] | None:
        if self.analysis is None:
            return None
        sug = engine.suggest_day_start(self.analysis.shots, self.day_start)
        return midnight_banner(self.analysis.shots, self.day_start, sug)

    def banner_day_start(self) -> time | None:
        if self.analysis is None:
            return None
        return engine.suggest_day_start(self.analysis.shots, self.day_start)


def _sanitize_reasons(text: str) -> list[str]:
    """Why the folder name differs from what was typed (the same rules name the folder)."""
    text = text.strip()
    if not text:
        return []
    return sanitize_component(text)[1]


def confirm_text(plan: ImportPlan) -> str:
    """``Import 455 files (61.2 GB) into 1 new folder?`` plus what is skipped."""
    c = plan.counts()
    new = sum(1 for t in plan.targets if not t.target.exists and t.files)
    old = sum(1 for t in plan.targets if t.target.exists and t.files)
    where = []
    if new:
        where.append(f"{plural(new, 'new folder')}")
    if old:
        where.append(f"{plural(old, 'existing folder')}")
    into = f" into {' and '.join(where)}" if where else ""
    head = f"Import {plural(len(plan.to_copy), 'file')} ({fmt_bytes(plan.bytes_to_copy)}){into}?"
    extras = []
    already = c.get(Action.SKIP_IMPORTED, 0) + c.get(Action.SKIP_EXISTS, 0)
    if already:
        extras.append(f"{already} already imported will be skipped")
    if plan.renamed:
        extras.append(f"{plan.renamed} renamed")
    if c.get(Action.SKIP_DUPLICATE):
        extras.append(f"{c[Action.SKIP_DUPLICATE]} identical on both cards copied once")
    if c.get(Action.SKIP_CULLED):
        extras.append(f"{c[Action.SKIP_CULLED]} removed from the library not imported again")
    if c.get(Action.UNKNOWN):
        extras.append(f"{c[Action.UNKNOWN]} unknown not imported")
    if not extras:
        return head
    return head + "\n\n" + ", ".join(extras).capitalize() + "."


# ---- preview, summary and delete rows -------------------------------------------------------


@dataclass(frozen=True)
class PreviewRow:
    action: str
    source: str
    dest: str
    taken: str
    size: str
    note: str = ""


def _rel(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _source_text(item: PlanItem) -> str:
    return f"{item.src.source.label}: {item.src.rel}"


def plan_rows(plan: ImportPlan) -> list[PreviewRow]:
    rows: list[PreviewRow] = []
    for item in plan.items:
        dest = item.dest
        if item.action in (Action.SKIP_IMPORTED, Action.SKIP_EXISTS) and item.existing:
            dest = item.existing
        action = item.action.value.capitalize()
        if item.action is Action.COPY and (item.renamed or item.variant):
            action = "Copy (renamed)"
        rows.append(PreviewRow(action, _source_text(item), _rel(dest, plan.dest_root),
                               fmt_taken(item.capture), fmt_bytes(item.src.size), item.reason))
    return rows


RESULT_GROUPS: dict[str, tuple[str, frozenset[Outcome]]] = {
    "copied": ("Copied and verified", frozenset({Outcome.COPIED})),
    "renamed": ("Renamed to avoid a name clash", frozenset({Outcome.COPIED})),
    "already": ("Already in the library", frozenset({Outcome.ALREADY_THERE})),
    "duplicate": ("Identical on both cards (copied once)", frozenset({Outcome.DUPLICATE})),
    "culled": ("Removed from the library earlier (not imported again)",
               frozenset({Outcome.CULLED})),
    "not_imported": ("Not imported (unknown type or day not selected)",
                     frozenset({Outcome.NOT_IMPORTED})),
    "cancelled": ("Not copied (cancelled)", frozenset({Outcome.CANCELLED})),
    "failed": ("Failed", frozenset({Outcome.FAILED})),
}


def _in_group(group: str, item: PlanItem, res: ItemResult) -> bool:
    if res.outcome not in RESULT_GROUPS[group][1]:
        return False
    return group != "renamed" or item.renamed


def result_rows(report: ImportReport, group: str | None = None) -> list[PreviewRow]:
    rows: list[PreviewRow] = []
    root = report.plan.dest_root
    for item in report.plan.items:
        res = report.results.get(item.id)
        if res is None or (group is not None and not _in_group(group, item, res)):
            continue
        note = res.error or item.reason
        rows.append(PreviewRow(res.outcome.value.capitalize(), _source_text(item),
                               _rel(res.dest or item.dest, root), fmt_taken(item.capture),
                               fmt_bytes(item.src.size), note))
    return rows


@dataclass(frozen=True)
class SummaryRow:
    group: str
    label: str
    count: int
    extra: str = ""


def summary_rows(report: ImportReport) -> list[SummaryRow]:
    counts: Counter[str] = Counter()
    for item in report.plan.items:
        res = report.results.get(item.id)
        if res is None:
            continue
        for g in RESULT_GROUPS:
            if _in_group(g, item, res):
                counts[g] += 1
    rows: list[SummaryRow] = []
    for g, (label, _) in RESULT_GROUPS.items():
        if g == "copied":
            rows.append(SummaryRow(g, label, counts[g], fmt_bytes(report.bytes_copied)))
        elif counts[g]:
            rows.append(SummaryRow(g, label, counts[g]))
    return rows


def summary_status(report: ImportReport) -> tuple[str, str]:
    """``(text, level)`` with level ok | warning | error."""
    failed = report.counts().get(Outcome.FAILED, 0)
    if report.status == "ok":
        return "✓ Import finished", "ok"
    if report.status == "partial":
        return f"{WARN} Import finished with {plural(failed, 'error')}", "warning"
    if report.status == "cancelled":
        return "Import cancelled. Files already copied stay; run Import again to finish.", \
            "warning"
    return f"Import stopped: {report.abort_reason or 'unknown error'}", "error"


def can_delete(report: ImportReport | None) -> bool:
    return report is not None and report.status in ("ok", "partial")


def deletion_rows(dplan: DeletionPlan) -> tuple[list[tuple[str, str, str]],
                                                list[tuple[str, str]]]:
    """``([(card, files, size)], [(file, reason)])`` for the delete dialog."""
    per = [(label, plural(n, "file"), fmt_bytes(b))
           for label, (n, b) in sorted(dplan.per_source.items())]
    excluded = [(f"{src.source.label}: {src.rel}", reason) for src, reason in dplan.excluded]
    return per, excluded
