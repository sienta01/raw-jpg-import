from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath

import pytest

from raw_jpg_import.formats import FormatTable, split_ext
from raw_jpg_import.grouping import (
    DayChoice,
    EventTarget,
    ExistingEvent,
    day_key,
    find_existing_events,
    group_days,
    resolve_targets,
    suggest_day_start,
    suggestions_for,
)
from raw_jpg_import.models import Capture, Source, SourceFile
from raw_jpg_import.shots import Shot, build_shots

CARD_A = Source(root=Path("X:/"), label="A", kind="card", order=0)
CARD_B = Source(root=Path("Y:/"), label="B", kind="card", order=1)
D23, D24, D25 = date(2026, 1, 23), date(2026, 1, 24), date(2026, 1, 25)
MIDNIGHT = time(0)


def at(day: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 1, day, hh, mm, ss)


def bare_shot(t: datetime, n: int = 0) -> Shot:
    """A shot with no members: enough for time-only logic."""
    return Shot(id=n, stem=f"DSC_{n:04d}", stem_key=f"dsc_{n:04d}",
                capture=Capture(local=t, source="exif", exact=True), members=[])


def bare_shots(start: datetime, end: datetime, step: timedelta) -> list[Shot]:
    out: list[Shot] = []
    t = start
    while t <= end:
        out.append(bare_shot(t, len(out)))
        t += step
    return out


def source_file(fmt: FormatTable, name: str, src: Source = CARD_A, size: int = 100,
                folder: str = "DCIM/100NCZ_F") -> SourceFile:
    rel = PurePosixPath(folder) / name
    stem, ext = split_ext(name)
    return SourceFile(source=src, path=src.root / rel, rel=rel, name=name, stem=stem, ext=ext,
                      size=size, mtime_ns=0, readonly=False, category=fmt.classify(name))


# ---- day_key ---------------------------------------------------------------------------------


def test_day_key_with_midnight_start():
    assert day_key(at(23, 23, 59, 59), MIDNIGHT) == D23
    assert day_key(at(24, 0, 0), MIDNIGHT) == D24
    assert day_key(at(24, 0, 31), MIDNIGHT) == D24


def test_day_key_with_four_am_start():
    four = time(4)
    assert day_key(at(24, 3, 59, 59), four) == D23
    assert day_key(at(24, 4, 0), four) == D24
    assert day_key(at(23, 23, 2), four) == D23
    assert day_key(at(24, 1, 29), time(1, 30)) == D23


# ---- group_days ------------------------------------------------------------------------------


def test_group_days_counts_bytes_and_flags(fmt):
    files = [
        source_file(fmt, "DSC_0001.NEF", size=30_000),
        source_file(fmt, "DSC_0001.WAV", size=50),
        source_file(fmt, "DSC_0001.JPG", CARD_B, size=9_000),
        source_file(fmt, "DSC_0002.NEF", size=31_000),
        source_file(fmt, "DSC_0002.NEF", CARD_B, size=31_000),     # backup duplicate
        source_file(fmt, "DSC_0003.JPG", size=5_000),
        source_file(fmt, "DSC_0003.JPG", CARD_B, size=2_000),      # variant -v2
        source_file(fmt, "DSC_0004.MOV", size=900_000),
        source_file(fmt, "DSC_0005.NEF", size=1),
    ]
    times = {
        "DSC_0001": Capture(at(23, 23, 2), "exif", True),
        "DSC_0002": Capture(at(23, 23, 40), "exif", True),
        "DSC_0003": Capture(at(24, 0, 10), "mtime", False),
        "DSC_0004": Capture(at(24, 0, 31), "mvhd+mtime", False),
        "DSC_0005": Capture(datetime(2000, 1, 1, 0, 0), "exif", True, suspicious=True),
    }
    caps = {f.path: times[f.stem] for f in files if f.ext != "wav"}
    shots, _ = build_shots(files, caps)

    groups = group_days(shots, MIDNIGHT)
    assert [g.day for g in groups] == [date(2000, 1, 1), D23, D24]
    old, g23, g24 = groups
    assert (old.suspicious, old.fallback_dates, dict(old.counts)) == (1, 0, {"RAW": 1})
    assert [s.stem for s in g23.shots] == ["DSC_0001", "DSC_0002"]
    assert (g23.first, g23.last) == (at(23, 23, 2), at(23, 23, 40))
    assert dict(g23.counts) == {"RAW": 2, "JPG": 1}
    assert g23.bytes == 30_000 + 50 + 9_000 + 31_000
    assert (g23.fallback_dates, g23.suspicious) == (0, 0)
    assert dict(g24.counts) == {"JPG": 2, "MOV": 1}
    assert g24.bytes == 5_000 + 2_000 + 900_000
    assert (g24.first, g24.last, g24.fallback_dates) == (at(24, 0, 10), at(24, 0, 31), 2)

    one = group_days(shots, time(1))
    assert [g.day for g in one] == [date(1999, 12, 31), D23]
    assert dict(one[1].counts) == {"RAW": 2, "JPG": 3, "MOV": 1}
    assert [s.id for s in one[1].shots] == sorted(s.id for s in one[1].shots)


def test_group_days_empty():
    assert group_days([], MIDNIGHT) == []


# ---- suggest_day_start -----------------------------------------------------------------------


def test_suggest_sunset_walk_past_midnight():
    shots = bare_shots(at(23, 23, 2), at(24, 0, 31), timedelta(minutes=3))
    shots.append(bare_shot(at(24, 0, 31), 99))
    assert suggest_day_start(shots, MIDNIGHT) == time(1)


def test_suggest_ending_one_minute_before_the_hour():
    shots = bare_shots(at(23, 23, 30), at(24, 0, 59), timedelta(minutes=1))
    assert suggest_day_start(shots, MIDNIGHT) == time(1)


def test_suggest_long_night():
    shots = bare_shots(at(23, 21, 0), at(24, 4, 30), timedelta(minutes=30))
    assert suggest_day_start(shots, MIDNIGHT) == time(5)


def test_suggest_none_when_gap_over_an_hour():
    shots = [bare_shot(at(23, 22, 59), 0), bare_shot(at(24, 0, 0), 1),
             bare_shot(at(24, 0, 30), 2)]
    assert suggest_day_start(shots, MIDNIGHT) is None
    shots[0] = bare_shot(at(23, 23, 0), 0)          # exactly 60 min: same cluster
    assert suggest_day_start(shots, MIDNIGHT) == time(1)


def test_suggest_none_when_shooting_ends_after_the_cap():
    shots = bare_shots(at(23, 23, 0), at(24, 7, 0), timedelta(minutes=30))
    assert suggest_day_start(shots, MIDNIGHT) is None


def test_suggest_clamped_to_the_cap():
    shots = bare_shots(at(23, 23, 30), at(24, 5, 30), timedelta(minutes=30))
    shots.append(bare_shot(at(24, 5, 59, 30), 99))
    assert suggest_day_start(shots, MIDNIGHT) == time(6)


def test_suggest_none_when_not_later_than_current_start():
    shots = bare_shots(at(23, 23, 2), at(24, 0, 31), timedelta(minutes=1))
    assert suggest_day_start(shots, time(1)) is None
    assert suggest_day_start(shots, time(2)) is None


def test_suggest_largest_candidate_wins():
    shots = bare_shots(at(23, 23, 2), at(24, 0, 31), timedelta(minutes=1))
    shots += bare_shots(at(24, 22, 0), at(25, 2, 10), timedelta(minutes=10))
    shots += bare_shots(at(25, 12, 0), at(25, 13, 0), timedelta(minutes=10))
    assert suggest_day_start(shots, MIDNIGHT) == time(3)
    assert suggest_day_start(shots, time(1)) == time(3)


def test_suggest_none_without_shots_or_crossing():
    assert suggest_day_start([], MIDNIGHT) is None
    assert suggest_day_start(bare_shots(at(23, 10, 0), at(23, 18, 0), timedelta(minutes=20)),
                             MIDNIGHT) is None


# ---- existing events -------------------------------------------------------------------------


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    root = tmp_path / "Pictures Ünïcode"
    for name in ("2026.01.23 - Sunset Walk", "2026.01.20 - Japan Trip", "2026.01.24",
                 "Notes", "2026.13.01 - Bad Date", "2026-01-23 - Wrong Format"):
        (root / name).mkdir(parents=True)
    (root / "2026.01.25 - Just A File").write_text("x")
    return root


def test_find_existing_events(dest):
    events = find_existing_events(dest)
    assert [(e.folder, e.date, e.name, e.days) for e in events] == [
        ("2026.01.20 - Japan Trip", date(2026, 1, 20), "Japan Trip", {date(2026, 1, 20)}),
        ("2026.01.23 - Sunset Walk", D23, "Sunset Walk", {D23}),
        ("2026.01.24", D24, "", {D24}),
    ]
    assert events[0].path == dest / "2026.01.20 - Japan Trip"


def test_find_existing_events_missing_root(tmp_path):
    assert find_existing_events(tmp_path / "nope") == []


def test_ledger_days_make_a_trip_folder_suggested_for_day_three(dest):
    ledger_days = {"2026.01.20 - JAPAN TRIP": {date(2026, 1, 21), date(2026, 1, 22)},
                   "2026.01.19 - Deleted Folder": {date(2026, 1, 22)}}
    events = find_existing_events(dest, ledger_days)
    trip = events[0]
    assert trip.days == {date(2026, 1, 20), date(2026, 1, 21), date(2026, 1, 22)}
    assert suggestions_for(date(2026, 1, 22), events) == [trip]
    assert suggestions_for(date(2026, 1, 26), events) == []


def test_suggestions_same_date_first_then_newest(tmp_path):
    def ev(folder: str, d: date, *days: date) -> ExistingEvent:
        return ExistingEvent(path=tmp_path / folder, folder=folder, date=d, name="x",
                             days=frozenset({d, *days}))

    trip = ev("2026.01.20 - Trip", date(2026, 1, 20), D23)
    later = ev("2026.01.22 - Tour", date(2026, 1, 22), D23)
    same_b = ev("2026.01.23 - b", D23)
    same_a = ev("2026.01.23 - A", D23)
    assert suggestions_for(D23, [trip, same_b, later, same_a]) == [same_a, same_b, later, trip]


# ---- resolve_targets -------------------------------------------------------------------------


def resolve(dest: Path, *choices: DayChoice, merge_all: bool = False,
            ledger_days: dict[str, set[date]] | None = None,
            ) -> tuple[dict[date, EventTarget], list[str]]:
    return resolve_targets(choices, dest, find_existing_events(dest, ledger_days),
                           merge_all=merge_all)


def test_new_folder_from_event_name(tmp_path):
    targets, warnings = resolve(tmp_path, DayChoice(D23, text="  Sunset: Beach/Walk? "))
    assert targets == {D23: EventTarget(path=tmp_path / "2026.01.23 - Sunset- Beach-Walk",
                                        folder="2026.01.23 - Sunset- Beach-Walk",
                                        exists=False, days=frozenset({D23}))}
    assert warnings == []


def test_empty_text_gives_date_only_folder(tmp_path):
    targets, warnings = resolve(tmp_path, DayChoice(D23), DayChoice(D24, text="   "))
    assert [t.folder for t in targets.values()] == ["2026.01.23", "2026.01.24"]
    assert warnings == []


def test_join_previous_shares_the_folder(tmp_path):
    targets, _ = resolve(tmp_path, DayChoice(D23, text="Sunset"),
                         DayChoice(D24, text="ignored", join_previous=True),
                         DayChoice(D25, text="Next"))
    assert targets[D23] is targets[D24]
    assert targets[D23].folder == "2026.01.23 - Sunset"
    assert targets[D23].days == {D23, D24}
    assert targets[D25].folder == "2026.01.25 - Next"


def test_join_previous_on_first_row_is_ignored(tmp_path):
    targets, _ = resolve(tmp_path, DayChoice(D24, text="Second", join_previous=True),
                         DayChoice(D23, text="First", join_previous=True))
    assert targets[D23].folder == "2026.01.23 - First"
    assert targets[D24] is targets[D23]


def test_join_previous_skips_excluded_days(tmp_path):
    targets, _ = resolve(tmp_path, DayChoice(D23, text="Trip"),
                         DayChoice(D24, include=False, text="Skipped"),
                         DayChoice(D25, join_previous=True))
    assert set(targets) == {D23, D25}
    assert targets[D25].folder == "2026.01.23 - Trip"
    assert targets[D25].days == {D23, D25}


def test_merge_all_uses_the_earliest_day(tmp_path):
    targets, _ = resolve(tmp_path, DayChoice(D25, text="Late"), DayChoice(D23, text="Trip"),
                         DayChoice(D24), merge_all=True)
    assert len({id(t) for t in targets.values()}) == 1
    assert targets[D25].folder == "2026.01.23 - Trip"
    assert targets[D25].days == {D23, D24, D25}
    assert not targets[D25].exists


def test_text_equal_to_an_existing_folder_reuses_it(dest):
    targets, warnings = resolve(dest, DayChoice(date(2026, 1, 22),
                                                text=" 2026.01.20 - japan TRIP "))
    t = targets[date(2026, 1, 22)]
    assert (t.folder, t.path, t.exists) == ("2026.01.20 - Japan Trip",
                                            dest / "2026.01.20 - Japan Trip", True)
    assert t.days == {date(2026, 1, 22)}
    assert warnings == []


def test_event_name_matching_an_existing_folder_case_insensitively(dest):
    targets, _ = resolve(dest, DayChoice(D23, text="sunset walk"))
    assert (targets[D23].folder, targets[D23].exists) == ("2026.01.23 - Sunset Walk", True)


def test_nfd_folder_on_disk_is_reused(tmp_path):
    nfd = unicodedata.normalize("NFD", "2026.01.23 - Café")
    (tmp_path / nfd).mkdir()
    targets, _ = resolve(tmp_path, DayChoice(D23, text="Café"))
    assert targets[D23].exists
    assert targets[D23].path == tmp_path / targets[D23].folder
    assert targets[D23].path.is_dir()


def test_on_disk_folder_missing_from_existing_list_is_reused(tmp_path):
    (tmp_path / "2026.01.23 - SUNSET").mkdir()
    targets, _ = resolve_targets([DayChoice(D23, text="Sunset")], tmp_path, [])
    assert (targets[D23].folder, targets[D23].exists) == ("2026.01.23 - SUNSET", True)


def test_full_event_folder_text_is_used_verbatim(tmp_path):
    targets, _ = resolve(tmp_path, DayChoice(D23, text="2026.01.25 - Party: Night"))
    assert targets[D23].folder == "2026.01.25 - Party- Night"
    targets, _ = resolve(tmp_path, DayChoice(D23, text="2026.01.20"))
    assert targets[D23].folder == "2026.01.20"


def test_groups_ending_on_the_same_folder_merge(dest):
    targets, _ = resolve(dest, DayChoice(D23, text="Sunset Walk"),
                         DayChoice(D24, text="2026.01.23 - SUNSET WALK"),
                         DayChoice(D25, text="2026.01.20 - Japan Trip"))
    assert targets[D23] is targets[D24]
    assert targets[D23].days == {D23, D24}
    assert targets[D23].folder == "2026.01.23 - Sunset Walk"
    assert targets[D25].folder == "2026.01.20 - Japan Trip"


def test_existing_trip_folder_for_day_three(dest):
    ledger_days = {"2026.01.20 - Japan Trip": {date(2026, 1, 21), date(2026, 1, 22)}}
    existing = find_existing_events(dest, ledger_days)
    day3 = date(2026, 1, 22)
    suggested = suggestions_for(day3, existing)[0]
    targets, warnings = resolve_targets([DayChoice(day3, text=suggested.folder)], dest,
                                        existing)
    assert (targets[day3].folder, targets[day3].exists) == ("2026.01.20 - Japan Trip", True)
    assert warnings == []


def test_warning_for_new_date_only_folder_next_to_named_ones(dest):
    targets, warnings = resolve(dest, DayChoice(D23))
    assert (targets[D23].folder, targets[D23].exists) == ("2026.01.23", False)
    assert len(warnings) == 1
    assert "2026.01.23 - Sunset Walk" in warnings[0]

    targets, warnings = resolve(dest, DayChoice(D24))      # date-only folder already exists
    assert (targets[D24].folder, targets[D24].exists, warnings) == ("2026.01.24", True, [])

    _, warnings = resolve(dest, DayChoice(D23, text="Another Event"))
    assert warnings == []


def test_warning_when_target_name_is_a_file(dest):
    targets, warnings = resolve(dest, DayChoice(D25, text="Just a file"))
    assert targets[D25].exists is False
    assert any("not a folder" in w for w in warnings)


def test_excluded_days_are_absent(tmp_path):
    targets, warnings = resolve(tmp_path, DayChoice(D23, include=False, text="x"))
    assert (targets, warnings) == ({}, [])


def test_targets_are_sorted_by_day(tmp_path):
    targets, _ = resolve(tmp_path, DayChoice(D25), DayChoice(D23), DayChoice(D24))
    assert list(targets) == [D23, D24, D25]
