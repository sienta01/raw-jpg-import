"""Group shots by day and resolve the event folder each day is imported into."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .naming import event_folder_name, format_date, key, parse_event_folder
from .shots import Shot


def day_key(t: datetime, day_start: time) -> date:
    """The calendar day a capture belongs to when days start at ``day_start``."""
    return (t - timedelta(hours=day_start.hour, minutes=day_start.minute)).date()


# ---- day groups ------------------------------------------------------------------------------


@dataclass
class DayGroup:
    day: date
    shots: list[Shot]
    first: datetime                 # earliest shot capture.local
    last: datetime                  # latest shot capture.local
    counts: Counter[str] = field(default_factory=Counter)   # folder -> copyable primaries
    bytes: int = 0                  # copyable members, sidecars included
    fallback_dates: int = 0         # shots whose capture is not exact
    suspicious: int = 0             # shots whose capture is suspicious


def group_days(shots: Sequence[Shot], day_start: time) -> list[DayGroup]:
    """Shots bucketed by :func:`day_key`, sorted by day (shots by capture time, then id)."""
    by_day: dict[date, list[Shot]] = defaultdict(list)
    for shot in shots:
        by_day[day_key(shot.capture.local, day_start)].append(shot)
    groups: list[DayGroup] = []
    for day in sorted(by_day):
        members = sorted(by_day[day], key=lambda s: (s.capture.local, s.id))
        g = DayGroup(day=day, shots=members, first=members[0].capture.local,
                     last=members[-1].capture.local)
        for shot in members:
            for m in shot.copyable():
                if m.kind == "primary":
                    g.counts[m.folder] += 1
                g.bytes += m.file.size
            g.fallback_dates += not shot.capture.exact
            g.suspicious += shot.capture.suspicious
        groups.append(g)
    return groups


def suggest_day_start(shots: Sequence[Shot], day_start: time,
                      gap: timedelta = timedelta(minutes=60), cap: time = time(6)) -> time | None:
    """A later day start that keeps shoots running past midnight in one day, or None.

    Captures are split into clusters at gaps over ``gap``. A cluster that crosses a day
    boundary and ends before ``cap`` suggests its last time plus one minute, rounded up to
    the hour (23:02..00:31 -> 01:00). The largest suggestion wins if it is later than
    ``day_start``.
    """
    times = sorted(s.capture.local for s in shots)
    best: time | None = None
    start = 0
    for i in range(1, len(times) + 1):
        if i < len(times) and times[i] - times[i - 1] <= gap:
            continue
        first, last = times[start], times[i - 1]
        start = i
        if day_key(first, day_start) == day_key(last, day_start) or last.time() >= cap:
            continue
        cand = min(_ceil_hour(last + timedelta(minutes=1)), cap)
        if best is None or cand > best:
            best = cand
    return best if best is not None and best > day_start else None


def _ceil_hour(t: datetime) -> time:
    whole = t.replace(minute=0, second=0, microsecond=0)
    if whole != t:
        whole += timedelta(hours=1)
    return whole.time()


# ---- existing event folders ------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingEvent:
    path: Path
    folder: str                     # real on-disk name
    date: date
    name: str                       # event name after the date ("" for date-only folders)
    days: frozenset[date]           # folder date + days the ledger recorded imports into it


def find_existing_events(dest_root: Path, ledger_days: Mapping[str, set[date]] | None = None,
                         ) -> list[ExistingEvent]:
    """Top-level event folders of ``dest_root`` (``YYYY.MM.DD[ - Name]``), by date and name.

    ``ledger_days`` maps event folder names to the capture dates imported into them, which
    makes multi-day trip folders discoverable for later days.
    """
    known: dict[str, set[date]] = defaultdict(set)
    for folder, days in (ledger_days or {}).items():
        known[key(folder)].update(days)
    events: list[ExistingEvent] = []
    for name, is_dir in _list_dir(dest_root):
        parsed = parse_event_folder(name)
        if not is_dir or parsed is None:
            continue
        d, event_name = parsed
        events.append(ExistingEvent(path=dest_root / name, folder=name, date=d,
                                    name=event_name, days=frozenset({d, *known[key(name)]})))
    events.sort(key=lambda e: (e.date, key(e.folder)))
    return events


def suggestions_for(day: date, existing: Sequence[ExistingEvent]) -> list[ExistingEvent]:
    """Existing events that hold ``day``: folders dated that day first, then newest first."""
    hits = [e for e in existing if day in e.days]
    return sorted(hits, key=lambda e: (e.date != day, -e.date.toordinal(), key(e.folder)))


def _list_dir(root: Path) -> list[tuple[str, bool]]:
    """``(name, is_dir)`` for the entries of ``root``; empty if it is missing or unreadable."""
    try:
        with os.scandir(root) as it:
            return [(e.name, _is_dir(e)) for e in it]
    except OSError:
        return []


def _is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


# ---- target resolution -----------------------------------------------------------------------


@dataclass
class DayChoice:
    day: date
    include: bool = True
    text: str = ""                  # event name, full folder name, or an existing folder
    join_previous: bool = False     # import into the previous included day's folder
    standalone: bool = False        # never joined or merged with other days (a day that is
                                    # only planned so its already-imported files show up)


@dataclass(frozen=True)
class EventTarget:
    path: Path
    folder: str
    exists: bool
    days: frozenset[date]


def resolve_targets(choices: Sequence[DayChoice], dest_root: Path,
                    existing: Sequence[ExistingEvent], *, merge_all: bool = False,
                    ) -> tuple[dict[date, EventTarget], list[str]]:
    """The event folder for every included day, plus warnings for the user.

    Days joined to the previous day (or all days, with ``merge_all``) form a group whose
    first day's text decides the folder. Groups that end on the same folder share one
    target. Existing folders are reused under their real on-disk name.
    """
    on_disk = {key(name): (name, is_dir) for name, is_dir in _list_dir(dest_root)}
    by_key = {key(e.folder): e for e in existing}
    resolved: dict[str, tuple[str, Path, bool]] = {}     # folder key -> (folder, path, exists)
    days_of: dict[str, set[date]] = defaultdict(set)
    warnings: list[str] = []

    for group in _day_groups(choices, merge_all):
        folder = _folder_for(group[0], by_key)
        fk = key(folder)
        if fk not in resolved:
            if fk in by_key:
                e = by_key[fk]
                resolved[fk] = (e.folder, e.path, True)
            elif fk in on_disk and on_disk[fk][1]:
                real = on_disk[fk][0]
                resolved[fk] = (real, dest_root / real, True)
            else:
                if fk in on_disk:
                    warnings.append(f"'{on_disk[fk][0]}' exists in the destination "
                                    "but is not a folder")
                resolved[fk] = (folder, dest_root / folder, False)
        days_of[fk].update(c.day for c in group)

    targets: dict[date, EventTarget] = {}
    for fk, (folder, path, exists) in resolved.items():
        target = EventTarget(path=path, folder=folder, exists=exists,
                             days=frozenset(days_of[fk]))
        targets.update(dict.fromkeys(target.days, target))
        if not exists:
            warnings.extend(_date_only_warning(folder, existing))
    return dict(sorted(targets.items())), warnings


def _day_groups(choices: Sequence[DayChoice], merge_all: bool) -> list[list[DayChoice]]:
    """Included days in date order, split into runs that share a folder.

    A ``standalone`` day is a group of its own and is skipped over by joins and merges.
    """
    seen: set[date] = set()
    groups: list[list[DayChoice]] = []
    apart: list[list[DayChoice]] = []
    for c in sorted((c for c in choices if c.include), key=lambda c: c.day):
        if c.day in seen:
            continue
        seen.add(c.day)
        if c.standalone:
            apart.append([c])
        elif groups and (merge_all or c.join_previous):
            groups[-1].append(c)
        else:
            groups.append([c])
    return groups + apart


def folder_name(day: date, text: str, existing: Sequence[ExistingEvent]) -> str:
    """The folder name :func:`resolve_targets` gives a day whose choice text is ``text``."""
    return _folder_for(DayChoice(day=day, text=text), {key(e.folder): e for e in existing})


def _folder_for(first: DayChoice, by_key: Mapping[str, ExistingEvent]) -> str:
    """The folder for a group, preferring an existing folder the text names.

    The text may be an event name, a full folder name or an existing folder. An existing
    folder is reused even when its name would not survive cleaning ("Rock--Paper",
    "Trip  2"), and also when the cleaned text matches its cleaned name.
    """
    text = first.text.strip()
    if text and key(text) in by_key:
        return by_key[key(text)].folder
    parsed = parse_event_folder(text)
    if parsed is not None:
        day, name = parsed
    else:
        day, name = first.day, text
        raw = f"{format_date(day)} - {text}" if text else format_date(day)
        if key(raw) in by_key:
            return by_key[key(raw)].folder
    folder = event_folder_name(day, name)
    if key(folder) in by_key:
        return by_key[key(folder)].folder
    for e in by_key.values():
        if e.date == day and key(event_folder_name(e.date, e.name)) == key(folder):
            return e.folder
    return folder


def _date_only_warning(folder: str, existing: Sequence[ExistingEvent]) -> list[str]:
    parsed = parse_event_folder(folder)
    if parsed is None or parsed[1]:
        return []
    others = [e.folder for e in suggestions_for(parsed[0], existing)]
    if not others:
        return []
    listed = ", ".join(f"'{f}'" for f in others)
    return [f"A new folder '{folder}' will be created although {listed} already "
            f"exist{'s' if len(others) == 1 else ''} for that date"]
