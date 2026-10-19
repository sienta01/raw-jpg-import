"""The GUI's rules without a display: formatting, names, joins, hiding, enabling, plans."""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

import pytest
from fakes.files import write_file
from fakes.jpeg import make_jpeg
from fakes.raw import make_nef

from raw_jpg_import import engine, volumes
from raw_jpg_import.config import load_config
from raw_jpg_import.events import Progress
from raw_jpg_import.grouping import ExistingEvent
from raw_jpg_import.gui import viewmodel as vm
from raw_jpg_import.gui.viewmodel import AppState, ChoiceState, SetupModel
from raw_jpg_import.planner import Action
from raw_jpg_import.volumes import Volume

D1, D2, D3 = date(2026, 1, 23), date(2026, 1, 24), date(2026, 1, 25)
SHOTS = [
    ("DSC_0001", datetime(2026, 1, 23, 17, 42, 0)),
    ("DSC_0002", datetime(2026, 1, 23, 23, 58, 0)),
    ("DSC_0003", datetime(2026, 1, 24, 0, 20, 0)),     # past midnight
    ("DSC_0004", datetime(2026, 1, 25, 10, 0, 0)),
]


@pytest.fixture(autouse=True)
def no_real_cards(monkeypatch):
    monkeypatch.setattr(volumes, "_mounts", lambda: [])


@pytest.fixture
def cfg():
    return load_config(use_user=False)


def make_card(root: Path, shots=SHOTS) -> Path:
    for stem, when in shots:
        folder = root / "DCIM" / "100NCZ_F"
        write_file(folder / f"{stem}.NEF", make_nef(when, name=f"{stem}.NEF"), when)
        write_file(folder / f"{stem}.JPG", make_jpeg(when, name=f"{stem}.JPG"), when)
    return root


def scanned_model(tmp_path: Path, cfg, **kw) -> SetupModel:
    m = SetupModel(**kw)
    m.folder_order = cfg.formats.folders()
    entry = m.add_folder(make_card(tmp_path / "card"))
    m.scan_finished(entry.key, engine.scan_source(entry.source, cfg))
    m.rebuild()
    return m


def plan_now(m: SetupModel, cfg) -> vm.PlanResult:
    result = vm.compute_plan(m.plan_request(cfg))
    m.apply_plan(result)
    return result


def event(folder: str, d: date, name: str, days=()) -> ExistingEvent:
    return ExistingEvent(path=Path("/x") / folder, folder=folder, date=d, name=name,
                         days=frozenset({d, *days}))


# ---- formatting -----------------------------------------------------------------------------


def test_day_and_time_formats():
    assert vm.fmt_day(D1) == "2026.01.23 Fri"
    assert vm.fmt_short_day(D2) == "Sat 24 Jan"
    assert vm.fmt_time_range(datetime(2026, 1, 23, 17, 42), datetime(2026, 1, 23, 23, 58)) \
        == "17:42-23:58"
    assert vm.fmt_hhmm(time(1, 0)) == "01:00"
    assert vm.parse_hhmm("02:00") == time(2, 0)
    assert vm.parse_hhmm("09:00", time(1)) == time(1)        # after 06:00: refused
    assert vm.parse_hhmm("junk") == time(0)
    assert vm.DAY_STARTS == ("00:00", "01:00", "02:00", "03:00", "04:00", "05:00", "06:00")


@pytest.mark.parametrize(("secs", "text"), [
    (0, "0 s"), (42, "42 s"), (60, "1 min"), (359, "6 min"), (3600, "1 h 00 min"),
    (3725, "1 h 02 min"),
])
def test_fmt_eta(secs, text):
    assert vm.fmt_eta(secs) == text


def test_progress_text_for_each_phase():
    copy = Progress("copy", 212, 455, 28_100_000_000, 61_200_000_000, "DSC_0212.NEF",
                    94_000_000, 359)
    assert vm.progress_text(copy) == ("212 / 455 files \u00b7 28.1 GB / 61.2 GB \u00b7 "
                                      "94.0 MB/s \u00b7 about 6 min left")
    assert vm.progress_fraction(copy) == pytest.approx(28.1 / 61.2)
    early = Progress("copy", 0, 1, 0, 10)
    assert vm.progress_text(early) == "0 / 1 file \u00b7 0 B / 10 B"
    assert vm.progress_text(Progress("scan", 1204, 0)) == "Scanning... 1204 files found"
    assert vm.progress_text(Progress("metadata", 340, 602)) == "Reading dates 340/602"
    assert vm.progress_text(Progress("delete", 3, 10)) == "3 / 10 files checked"
    assert vm.progress_fraction(Progress("delete", 3, 10)) == pytest.approx(0.3)
    assert vm.progress_fraction(Progress("scan", 3, 0)) == 0.0


def test_short_path_keeps_the_end():
    assert vm.short_path("C:\\short") == "C:\\short"
    long = "C:\\Users\\someone\\AppData\\Local\\Temp\\very\\deep\\folder\\cards\\SLOT1"
    s = vm.short_path(long, 30)
    assert len(s) == 30 and s.endswith("cards\\SLOT1") and "..." in s


def test_dest_path():
    assert vm.dest_path("") is None
    assert vm.dest_path("   ") is None
    assert vm.dest_path(' "C:\\Photos" ') == Path("C:\\Photos")
    if sys.platform == "win32":
        assert vm.dest_path("D:") == Path("D:\\")


def test_fmt_taken():
    from raw_jpg_import.models import Capture

    assert vm.fmt_taken(None) == ""
    assert vm.fmt_taken(Capture(datetime(2026, 1, 23, 17, 42, 5), "exif", True)) \
        == "2026-01-23 17:42:05"
    assert vm.fmt_taken(Capture(datetime(2026, 1, 23, 17, 42, 5), "mtime", False)) \
        == "2026-01-23 17:42:05 (file time)"


def test_viewmodel_does_not_import_tkinter():
    code = ("import sys, raw_jpg_import.gui.viewmodel; "
            "sys.exit(1 if 'tkinter' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_gui_main_without_tkinter_prints_an_install_hint():
    code = (
        "import sys\n"
        "class NoTk:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('tkinter', '_tkinter'):\n"
        "            raise ImportError('no tkinter here')\n"
        "sys.meta_path.insert(0, NoTk())\n"
        "from raw_jpg_import.gui.app import main\n"
        "sys.exit(main())\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 1
    assert "python3-tk" in r.stderr and "brew install python-tk" in r.stderr


# ---- names, suggestions and joins -----------------------------------------------------------


def test_prefill_rules():
    same_day = event("2026.01.23 - Sunset Beachwalk", D1, "Sunset Beachwalk")
    trip = event("2026.01.20 - Coast Trip", date(2026, 1, 20), "Coast Trip", days=[D1])
    date_only = event("2026.01.23", D1, "")
    assert vm.prefill_name(D1, [same_day]) == "Sunset Beachwalk"
    assert vm.prefill_name(D1, [trip]) == "2026.01.20 - Coast Trip"
    assert vm.prefill_name(D1, [date_only]) == ""
    assert vm.prefill_name(D1, [same_day, trip]) == ""
    assert vm.prefill_name(D1, []) == ""


def test_typed_names_win_over_suggestions():
    existing = [event("2026.01.23 - Sunset Beachwalk", D1, "Sunset Beachwalk")]
    assert vm.name_for(D1, existing, {}) == "Sunset Beachwalk"
    assert vm.name_for(D1, existing, {D1: "Party"}) == "Party"
    assert vm.name_for(D1, existing, {D1: ""}) == ""        # cleared on purpose


def test_combo_values_list_folders_then_recent_names_once():
    existing = [event("2026.01.23 - Sunset Beachwalk", D1, "Sunset Beachwalk"),
                event("2026.01.20 - Coast Trip", date(2026, 1, 20), "Coast Trip", days=[D1]),
                event("2026.02.01 - Other", date(2026, 2, 1), "Other")]
    values = vm.combo_values(D1, existing, ["Birthday", "2026.01.20 - coast trip", " "])
    assert values == ["2026.01.23 - Sunset Beachwalk", "2026.01.20 - Coast Trip", "Birthday"]


def test_choices_for_joins_only_after_an_included_day(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg)
    groups = m.groups
    assert [g.day for g in groups] == [D1, D2, D3]
    state = ChoiceState(names={D1: "Party"}, joins={D1: True, D3: True},
                        checked={D1: False, D2: True, D3: True})
    choices = vm.choices_for(groups, [], state)
    assert [(c.day, c.include, c.text, c.join_previous) for c in choices] == [
        (D1, False, "Party", False),        # not included: never joins
        (D2, True, "", False),              # first included day cannot join
        (D3, True, "", True),
    ]
    merged = vm.choices_for(groups, [], ChoiceState({}, {}, {}, merge_all=True))
    assert [c.join_previous for c in merged] == [False, True, True]


def test_merge_all_restores_the_previous_joins(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg)
    m.set_join(D3, True)
    assert [m.effective_join(d) for d in (D1, D2, D3)] == [False, False, True]
    m.set_merge_all(True)
    assert [m.effective_join(d) for d in (D1, D2, D3)] == [False, True, True]
    m.set_join(D2, False)                       # ignored while merged
    rows = m.rows()
    assert [r.join for r in rows] == [False, True, True]
    assert not any(r.join_enabled for r in rows)
    m.set_merge_all(False)
    assert m.joins == {D3: True}
    assert [m.effective_join(d) for d in (D1, D2, D3)] == [False, False, True]
    rows = m.rows()
    assert [r.join_enabled for r in rows] == [False, True, True]


def test_unchecked_rows_do_not_join(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg)
    m.set_join(D2, True)
    m.set_checked(D1, False)
    assert not m.effective_join(D2)             # nothing checked above it
    m.set_checked(D2, False)
    assert not m.effective_join(D2)


def test_rows_show_counts_and_prefill(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg, recent_names=["Birthday"])
    m.existing = [event("2026.01.23 - Sunset Beachwalk", D1, "Sunset Beachwalk")]
    rows = m.rows()
    assert m.columns() == ["RAW", "JPG"]
    first = rows[0]
    assert (first.label, first.shot, first.counts) == ("2026.01.23 Fri", "17:42-23:58",
                                                       {"RAW": 2, "JPG": 2})
    assert first.name == "Sunset Beachwalk"
    assert first.values == ["2026.01.23 - Sunset Beachwalk", "Birthday"]
    assert first.new is None and first.checked
    assert rows[1].values == ["Birthday"]


def test_typed_names_survive_regrouping(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg)
    m.set_name(D1, "Late party")
    assert m.set_day_start(time(1, 0))
    assert [g.day for g in m.groups] == [D1, D3]
    assert m.name(D1) == "Late party"
    assert not m.set_day_start(time(1, 0))


def test_midnight_banner(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg)
    msg, button = m.banner()
    assert "Shooting continued past midnight (00:20-00:20 on Sat 24 Jan)" in msg
    assert "01:00" in msg
    assert button == "Count as Fri 23 Jan"
    assert m.banner_day_start() == time(1, 0)
    m.set_day_start(time(1, 0))
    assert m.banner() is None
    assert vm.midnight_banner([], time(0), None) is None


# ---- sources --------------------------------------------------------------------------------


def test_cards_are_added_greyed_and_returned(tmp_path):
    m = SetupModel()
    folder = m.add_folder(tmp_path / "folder")
    a, b = tmp_path / "A", tmp_path / "B"
    va = Volume(a, "NIKON Z F", "1111-2222", 64, 32, True)
    vb = Volume(b, "EOS_DIGITAL", "3333-4444", 64, 32, True)
    ch = m.cards_changed([va, vb])
    assert [e.source.label for e in ch.added] == ["NIKON Z F", "EOS_DIGITAL"]
    assert all(e.is_card and e.checked and e.status == "waiting" for e in ch.added)
    assert [e.source.order for e in m.sources] == sorted(e.source.order for e in m.sources)
    assert not m.cards_changed([va, vb])
    ch = m.cards_changed([va])
    assert [e.source.label for e in ch.removed] == ["EOS_DIGITAL"]
    gone = ch.removed[0]
    assert not gone.present and gone.status_text(None) == "Card removed"
    assert folder.present                       # folders are never "removed"
    gone.status = "scanned"
    ch = m.cards_changed([va, vb])
    assert ch.returned == [gone] and gone.present and gone.status == "waiting"


def test_scan_queue_and_status(tmp_path, cfg):
    m = SetupModel()
    one = m.add_folder(make_card(tmp_path / "one", SHOTS[:1]))
    two = m.add_folder(make_card(tmp_path / "two", SHOTS[3:]))
    assert m.add_folder(tmp_path / "one") is one
    assert m.scanning and m.next_to_scan() is one
    assert one.title == "one (folder)"
    assert one.status_text(None) == "Waiting to scan"
    one.status = "scanning"
    assert m.next_to_scan() is two
    scan = engine.scan_source(one.source, cfg)
    m.scan_finished(one.key, scan)
    assert one.status_text(scan) == "\u2713 Scanned"
    assert vm.scan_summary(scan).startswith("2 files \u00b7 ")
    m.scan_failed(two.key, "boom")
    assert two.status_text(None) == "Could not scan: boom"
    assert not m.scanning
    assert m.selected_scans() == [scan]
    m.set_source_checked(one.key, False)
    assert m.selected_scans() == []
    m.mark_for_rescan()
    assert one.status == "waiting" and one.status_text(None) == "Not scanned"
    assert m.remove_source(one.key) is one and one.key not in m.scans


# ---- plan, hiding and enabling --------------------------------------------------------------


def test_compute_plan_without_a_destination(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg)
    r = plan_now(m, cfg)
    assert r.plan is None and not r.dest_ok
    assert m.dest_info() == "Choose the folder that holds your event folders."
    m.set_dest(str(tmp_path / "missing"))
    r = plan_now(m, cfg)
    assert r.plan is None and not m.dest_ok
    assert m.dest_info() == "This folder does not exist."
    assert not m.enabled(AppState.IDLE).import_


def test_live_plan_counts_new_files_per_day(tmp_path, cfg):
    dest = tmp_path / "dest"
    (dest / "2026.01.23 - Sunset Beachwalk").mkdir(parents=True)
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    r = plan_now(m, cfg)
    assert r.plan is not None and not r.plan.problems
    assert [e.folder for e in m.existing] == ["2026.01.23 - Sunset Beachwalk"]
    assert m.new_by_day == {D1: 4, D2: 2, D3: 2}
    assert m.name(D1) == "Sunset Beachwalk"
    assert m.import_label() == "Import 8 files"
    assert m.space_line().startswith("Needs ") and " of " in m.space_line()
    assert m.dest_info().endswith("1 event folder found")
    rows = m.destination_rows()
    assert [(r.status, r.folder, r.contents) for r in rows] == [
        ("EXISTING", "2026.01.23 - Sunset Beachwalk", "RAW 2 \u00b7 JPG 2"),
        ("NEW", "2026.01.24", "RAW 1 \u00b7 JPG 1"),
        ("NEW", "2026.01.25", "RAW 1 \u00b7 JPG 1"),
    ]
    assert m.enabled(AppState.IDLE).import_
    text = vm.confirm_text(r.plan)
    assert text.startswith("Import 8 files (")
    assert "into 2 new folders and 1 existing folder?" in text


def test_unchecked_days_keep_their_last_new_count(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    plan_now(m, cfg)
    m.set_checked(D2, False)
    r = plan_now(m, cfg)
    assert D2 not in r.new_by_day
    assert m.new_by_day[D2] == 2
    assert m.import_label() == "Import 6 files"


def test_sanitised_names_are_flagged(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    m.set_name(D1, "Beach: day?")
    m.set_join(D2, True)
    plan_now(m, cfg)
    row = m.destination_rows()[0]
    assert row.folder.startswith("2026.01.23 - Beach- day")
    assert "\u26a0" in row.folder and "replaced characters" in row.warning
    assert row.contents == "RAW 3 \u00b7 JPG 3"
    assert m.names_to_remember() == ["Beach: day?"]


def test_already_imported_days_are_hidden_and_unchecked(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    m.set_name(D1, "Party")
    r = plan_now(m, cfg)
    assert not m.pending_auto_hide and not m.hidden
    report = engine.execute(r.plan)
    assert report.status == "ok"

    m.rebuild()                                   # e.g. Run again: rescanned
    assert m.pending_auto_hide
    assert plan_now(m, cfg).plan is not None
    assert m.hidden == {D1, D2, D3}
    assert not any(m.is_checked(d) for d in (D1, D2, D3))
    assert m.rows() == []
    assert m.hidden_status() == "3 days already imported (hidden)"
    assert m.problems() == []                     # "No days selected" is not an error
    en = m.enabled(AppState.IDLE)
    assert not en.import_ and en.why_not == "Tick at least one day"

    m.set_hide_imported(False)
    assert not m.hidden and [r.checked for r in m.rows()] == [False, False, False]
    assert m.hidden_status() == ""
    m.set_hide_imported(True)                     # known New == 0 hides at once
    assert m.hidden == {D1, D2, D3}


def test_hidden_days_come_back_when_the_destination_changes(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    engine.execute(plan_now(m, cfg).plan)
    m.rebuild()
    plan_now(m, cfg)
    assert m.hidden == {D1, D2, D3}
    other = tmp_path / "other"
    other.mkdir()
    assert m.set_dest(str(other))
    assert all(m.is_checked(d) for d in (D1, D2, D3))     # re-evaluated, still hidden
    assert m.rows() == []
    changed = m.apply_plan(vm.compute_plan(m.plan_request(cfg)))
    assert not changed
    assert m.hidden == set() and len(m.rows()) == 3


def test_auto_hide_happens_once_per_context(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    plan_now(m, cfg)
    engine.execute(m.plan)
    # Same context (no rescan, no destination change): re-planning while the user types
    # must not make rows disappear under the cursor.
    assert not plan_now(m, cfg).plan.to_copy
    assert m.new_by_day == {D1: 0, D2: 0, D3: 0}
    assert m.hidden == set() and len(m.rows()) == 3


def test_reimport_culled_option(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest), hide_imported=False)
    m.set_name(D1, "Party")
    engine.execute(plan_now(m, cfg).plan)
    culled = dest / "2026.01.23 - Party" / "RAW" / "DSC_0001.NEF"
    culled.unlink()                               # the user deleted it from the library
    plan_now(m, cfg)
    assert m.culled_seen == 1
    assert m.plan.counts()[Action.SKIP_CULLED] == 1
    assert any("removed from the library" in n.text for n in m.notices())
    m.reimport_culled = True
    plan_now(m, cfg)
    assert m.culled_seen == 1                     # still shown while the option is on
    assert m.plan.counts()[Action.SKIP_CULLED] == 0
    assert m.import_label() == "Import 1 file"


def test_unknown_files_notice(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    card = make_card(tmp_path / "card")
    write_file(card / "DCIM" / "100NCZ_F" / "NOTES.XYZ", b"hello", datetime(2026, 1, 23, 18))
    m = SetupModel(dest_text=str(dest))
    entry = m.add_folder(card)
    m.scan_finished(entry.key, engine.scan_source(entry.source, cfg))
    m.rebuild()
    unknown = [n for n in m.notices() if n.action == "unknown"]
    assert unknown and unknown[0].text.startswith("1 file of unknown type")
    plan_now(m, cfg)
    unknown = [n for n in m.notices() if n.action == "unknown"]
    assert unknown[0].text == "1 file of unknown type won't be imported (.XYZ \u00d71)"
    assert "NOTES.XYZ" in m.unknown_details()


def test_problems_and_config_errors_block_import(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    plan_now(m, cfg)
    m.config_error = "bad line 3"
    en = m.enabled(AppState.IDLE)
    assert not en.import_ and "config" in en.why_not
    assert m.problems() == ["Config file error: bad line 3"]
    m.config_error = None
    m.plan_failed("disk vanished")
    assert m.problems() == ["Could not plan the import: disk vanished"]
    assert not m.enabled(AppState.IDLE).import_


@pytest.mark.parametrize(("kw", "why"), [
    (dict(state=AppState.IMPORTING), ""),
    (dict(scanning=True), "Scanning the cards..."),
    (dict(state=AppState.SCANNING), "Scanning the cards..."),
    (dict(dest_set=False), "Choose a destination folder"),
    (dict(any_checked=False), "Tick at least one day"),
    (dict(plan_pending=True), "Updating..."),
    (dict(plan=None), "Updating..."),
    (dict(blocked="Fix the config"), "Fix the config"),
])
def test_enabled_actions_reasons(kw, why, tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    plan_now(m, cfg)
    args = dict(state=AppState.IDLE, scanning=False, dest_set=True, plan=m.plan,
                plan_pending=False, blocked=None, any_checked=True)
    assert vm.enabled_actions(**args).import_
    args.update(kw)
    state = args.pop("state")
    en = vm.enabled_actions(state, **args)
    assert not en.import_ and en.why_not == why
    if state is AppState.IMPORTING:
        assert not en.edit and not en.settings and not en.preview


def test_enabled_actions_with_problems_or_nothing_to_copy(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    m = scanned_model(tmp_path, cfg, dest_text=str(dest))
    plan_now(m, cfg)
    plan = m.plan
    plan.problems.append("Not enough space")
    en = vm.enabled_actions(AppState.IDLE, scanning=False, dest_set=True, plan=plan,
                            plan_pending=False)
    assert en.why_not == "Fix the problems shown above" and en.preview
    plan.problems.clear()
    plan.items.clear()
    en = vm.enabled_actions(AppState.IDLE, scanning=False, dest_set=True, plan=plan,
                            plan_pending=False)
    assert en.why_not == "Nothing new to import"


def test_reset_days_forgets_choices(tmp_path, cfg):
    m = scanned_model(tmp_path, cfg)
    m.set_name(D1, "x")
    m.set_join(D2, True)
    m.set_merge_all(True)
    m.set_checked(D3, False)
    m.reimport_culled = True
    m.reset_days()
    assert (m.names_by_date, m.joins, m.merge_all, m.checked, m.reimport_culled) == \
        ({}, {}, False, {}, False)
    assert m.pending_auto_hide


# ---- preview, summary and delete rows -------------------------------------------------------


def test_preview_summary_and_delete_rows(tmp_path, cfg):
    dest = tmp_path / "dest"
    dest.mkdir()
    card = make_card(tmp_path / "card", SHOTS[:2])
    when = datetime(2026, 1, 23, 23, 59, 0)          # the counter rolled over: name clash
    folder = card / "DCIM" / "101NCZ_F"
    write_file(folder / "DSC_0001.NEF", make_nef(when, name="b.NEF"), when)
    write_file(folder / "DSC_0001.JPG", make_jpeg(when, name="b.JPG"), when)
    m = SetupModel(dest_text=str(dest))
    entry = m.add_folder(card)
    m.scan_finished(entry.key, engine.scan_source(entry.source, cfg))
    m.rebuild()
    m.set_name(D1, "Party")
    plan_now(m, cfg)
    rows = vm.plan_rows(m.plan)
    assert Counter(r.action for r in rows) == {"Copy": 4, "Copy (renamed)": 2}
    renamed = next(r for r in rows if r.action == "Copy (renamed)")
    assert renamed.source == "card: DCIM/101NCZ_F/DSC_0001.NEF"
    assert Path(renamed.dest) == Path("2026.01.23 - Party/RAW/DSC_0001-2.NEF")
    assert renamed.taken == "2026-01-23 23:59:00"
    assert renamed.note == "renamed: DSC_0001.NEF -> DSC_0001-2.NEF"

    report = engine.execute(m.plan)
    assert vm.summary_status(report) == ("\u2713 Import finished", "ok")
    summary = vm.summary_rows(report)
    assert [(s.group, s.count) for s in summary] == [("copied", 6), ("renamed", 2)]
    assert len(vm.result_rows(report)) == 6
    assert len(vm.result_rows(report, "renamed")) == 2
    assert vm.result_rows(report, "failed") == []
    assert vm.can_delete(report) and not vm.can_delete(None)
    per, excluded = vm.deletion_rows(engine.plan_deletion(report))
    assert per[0][0] == "card" and per[0][1] == "6 files" and excluded == []

    report.status = "partial"
    assert vm.summary_status(report)[1] == "warning"
    report.status = "cancelled"
    assert "run Import again" in vm.summary_status(report)[0]
    assert not vm.can_delete(report)
    report.status, report.abort_reason = "aborted", "The destination drive is full"
    assert vm.summary_status(report) == ("Import stopped: The destination drive is full",
                                         "error")
