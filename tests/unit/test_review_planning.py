"""Regression tests for reviewed planning, naming, ledger and CLI defects.

Each test names the review finding it covers. Cards are fake folders under ``tmp_path``
and card detection is blocked, so nothing here can reach a real card in the reader.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from fakes.bmff import make_mov, video_mtime
from fakes.card import (
    FakeCard,
    FakeShot,
    files_under,
    forget_history,
    make_plan,
    make_zf_cards,
    no_real_cards,
    run_import,
)
from fakes.files import write_file

from raw_jpg_import import cli, engine, metadata, volumes
from raw_jpg_import import ledger as ledger_mod
from raw_jpg_import.ledger import Ledger, Row, root_key
from raw_jpg_import.planner import Action, PlanOptions
from raw_jpg_import.volumes import Volume

D = datetime
WALK = "2026.01.23 - Walk"
ROOT = root_key(os.path.abspath("Photos Library"))


@pytest.fixture(autouse=True)
def _no_real_cards(monkeypatch):
    no_real_cards(monkeypatch)


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch):
    """No terminal on stdin, and the log files ``cli.main`` opens are closed afterwards."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    yield
    root = logging.getLogger("raw_jpg_import")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "lib"
    d.mkdir()
    return d


def copies(plan) -> dict[str, str]:
    """``{card file name: destination rel}`` for the COPY items of ``plan``."""
    return {i.src.name: i.dest_rel.as_posix() for i in plan.items if i.action is Action.COPY}


# ---- 1: library files without a capture of their own never force a rename -----------------


def test_editor_xmp_next_to_imported_nef_keeps_jpg_names(tmp_path, dest):
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 18, 0, 0)),
             FakeShot("DSC_0002", D(2026, 1, 23, 18, 5, 0))]
    s1, s2 = make_zf_cards(tmp_path / "cards", shots, "raw_jpg", junk=False)
    run_import([s1], dest, name="Walk")
    raw = dest / WALK / "RAW"
    (raw / "DSC_0001.xmp").write_text("<x:xmpmeta/>")         # Lightroom / ACR style
    (raw / "DSC_0002.NEF.xmp").write_text("<x:xmpmeta/>")     # darktable style

    plan = make_plan([s2], dest, name="Walk")
    assert copies(plan) == {"DSC_0001.JPG": "JPG/DSC_0001.JPG",
                            "DSC_0002.JPG": "JPG/DSC_0002.JPG"}
    both = make_plan([s1, s2], dest, name="Walk")
    assert copies(both) == copies(plan)
    assert not any("partner" in w for w in both.warnings)


def test_voice_memo_without_history_keeps_jpg_name(tmp_path, dest):
    when = D(2026, 1, 23, 18, 0, 0)
    s1, s2 = make_zf_cards(tmp_path / "cards", [FakeShot("DSC_0001", when)], "raw_jpg",
                           junk=False)
    FakeCard(s1).file("DCIM/100NCZ_F/DSC_0001.WAV", b"RIFF....WAVEfmt " + bytes(200),
                      when + timedelta(minutes=10))
    run_import([s1], dest, name="Walk")
    assert files_under(dest / WALK) == {"RAW/DSC_0001.NEF", "RAW/DSC_0001.WAV"}
    forget_history()
    assert copies(make_plan([s2], dest, name="Walk")) == {"DSC_0001.JPG": "JPG/DSC_0001.JPG"}


def test_a_different_photo_still_owns_its_stem(tmp_path, dest):
    """Only files that cannot tell their shot are ignored; a real photo still forces -2."""
    s1, _ = make_zf_cards(tmp_path / "a", [FakeShot("DSC_0001", D(2026, 1, 23, 10))], "raw_jpg",
                          junk=False)
    _, b2 = make_zf_cards(tmp_path / "b", [FakeShot("DSC_0001", D(2026, 1, 23, 15))], "raw_jpg",
                          junk=False)
    run_import([s1], dest, name="Walk")
    forget_history()
    assert copies(make_plan([b2], dest, name="Walk")) == {"DSC_0001.JPG": "JPG/DSC_0001-2.JPG"}


# ---- 2: a folder only holds the days its files belong to with the current day start --------


def test_ledger_days_follow_day_start():
    with Ledger.memory() as led:
        for n, when in enumerate(("2026-01-23T23:00:00", "2026-01-24T00:31:00")):
            led.record(Row(id=None, session="s", kind="copied", dest_root=ROOT, event="Party",
                           dest_rel=f"RAW/DSC_000{n}.NEF", orig_key=f"dsc_000{n}.nef", size=1,
                           mtime_ns=0, capture=when, capture_exact=True, subsec=None,
                           shot_stem_key=f"dsc_000{n}", sha256=None, card_label="C",
                           card_serial=None, src_rel=f"DCIM/DSC_000{n}.NEF"))
        assert led.days_by_event(ROOT) == {"Party": {date(2026, 1, 23), date(2026, 1, 24)}}
        assert led.days_by_event(ROOT, time(1, 0)) == {"Party": {date(2026, 1, 23)}}
        assert led.days_by_event(ROOT, time(0, 30)) == {
            "Party": {date(2026, 1, 23), date(2026, 1, 24)}}


def test_next_days_photos_are_not_filed_into_last_nights_party(tmp_path, dest, capsys):
    party = [FakeShot("DSC_0001", D(2026, 1, 23, 23, 0, 0)),
             FakeShot("DSC_0002", D(2026, 1, 24, 0, 31, 0))]
    nextday = [FakeShot("DSC_0003", D(2026, 1, 24, 14, 0, 0))]
    p1, p2 = make_zf_cards(tmp_path / "party", party, "raw_jpg", junk=False)
    n1, n2 = make_zf_cards(tmp_path / "next", nextday, "raw_jpg", junk=False)
    assert cli.main(["import", str(p1), str(p2), "--dest", str(dest), "--day-start", "01:00",
                     "--name", "Party", "--yes"]) == 0
    assert engine.existing_events(dest, day_start=time(1))[0].days == {date(2026, 1, 23)}
    capsys.readouterr()
    # No name and no terminal: the README says this stops and asks for a name.
    assert cli.main(["import", str(n1), str(n2), "--dest", str(dest)]) == 2
    assert "No event name for: 2026.01.24" in capsys.readouterr().err
    assert files_under(dest) == {f"2026.01.23 - Party/{p}" for p in (
        "RAW/DSC_0001.NEF", "JPG/DSC_0001.JPG", "RAW/DSC_0002.NEF", "JPG/DSC_0002.JPG")}


# ---- 3: --join / --merge reach into days that were imported in an earlier run --------------


def run_cli(*argv: object) -> int:
    return cli.main([str(a) for a in argv])


def test_join_to_a_day_imported_earlier(tmp_path, dest):
    day1 = [FakeShot("DSC_0001", D(2026, 1, 23, 10)), FakeShot("DSC_0002", D(2026, 1, 23, 11))]
    day2 = [FakeShot("DSC_0003", D(2026, 1, 24, 10)), FakeShot("DSC_0004", D(2026, 1, 24, 11))]
    s1, s2 = make_zf_cards(tmp_path / "cards", day1 + day2, "raw_jpg", junk=False)
    assert run_cli("import", s1, s2, "--dest", dest, "--days", "2026-01-23", "--name", "Trip",
                   "--yes") == 0
    assert run_cli("import", s1, s2, "--dest", dest, "--name", "2026-01-23=Trip",
                   "--join", "2026-01-24", "--yes") == 0
    assert {p.split("/")[0] for p in files_under(dest)} == {"2026.01.23 - Trip"}
    assert len(files_under(dest)) == 8


def test_join_uses_the_folder_holding_the_earlier_day(tmp_path, dest):
    """No name for the earlier day: the folder its files went into is the one joined."""
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 10)), FakeShot("DSC_0002", D(2026, 1, 24, 10))]
    s1, s2 = make_zf_cards(tmp_path / "cards", shots, "raw_jpg", junk=False)
    (dest / "2026.01.23 - Other").mkdir()           # a second folder dated that day
    assert run_cli("import", s1, s2, "--dest", dest, "--days", "2026-01-23", "--name",
                   "2026-01-23=Trip", "--yes") == 0
    assert run_cli("import", s1, s2, "--dest", dest, "--join", "2026-01-24") == 0
    assert files_under(dest / "2026.01.23 - Trip") == {
        "RAW/DSC_0001.NEF", "JPG/DSC_0001.JPG", "RAW/DSC_0002.NEF", "JPG/DSC_0002.JPG"}


def test_merge_run_again_on_a_later_day(tmp_path, dest):
    d2 = [FakeShot("DSC_0001", D(2026, 1, 23, 10)), FakeShot("DSC_0002", D(2026, 1, 24, 10))]
    d3 = d2 + [FakeShot("DSC_0003", D(2026, 1, 25, 10))]
    assert run_cli("import", *make_zf_cards(tmp_path / "c2", d2, "raw_jpg", junk=False),
                   "--dest", dest, "--merge", "--name", "Bali Trip", "--yes") == 0
    assert run_cli("import", *make_zf_cards(tmp_path / "c3", d3, "raw_jpg", junk=False),
                   "--dest", dest, "--merge", "--name", "Bali Trip", "--yes") == 0
    assert [p.name for p in dest.iterdir()] == ["2026.01.23 - Bali Trip"]
    assert "RAW/DSC_0003.NEF" in files_under(dest / "2026.01.23 - Bali Trip")


def test_merge_skips_an_imported_day_in_the_middle(tmp_path, dest):
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 10)), FakeShot("DSC_0002", D(2026, 1, 24, 10)),
             FakeShot("DSC_0003", D(2026, 1, 25, 10))]
    s1, s2 = make_zf_cards(tmp_path / "cards", shots, "raw_jpg", junk=False)
    assert run_cli("import", s1, s2, "--dest", dest, "--days", "2026-01-24", "--name", "Mid",
                   "--yes") == 0
    assert run_cli("import", s1, s2, "--dest", dest, "--merge", "--name", "Trip",
                   "--yes") == 0
    assert sorted(p.name for p in dest.iterdir()) == ["2026.01.23 - Trip", "2026.01.24 - Mid"]
    assert files_under(dest / "2026.01.23 - Trip") == {
        "RAW/DSC_0001.NEF", "JPG/DSC_0001.JPG", "RAW/DSC_0003.NEF", "JPG/DSC_0003.JPG"}


# ---- 8: a later card takes the renamed stem its partner already holds ---------------------


TRIP = "2026.01.23 - Trip"


@pytest.mark.parametrize("history", [True, False])
def test_partner_keeps_its_renamed_stem_after_a_cull(tmp_path, dest, history):
    a = FakeShot("DSC_0001", D(2026, 1, 23, 10, 0, 0))
    b = FakeShot("DSC_0001", D(2026, 1, 24, 10, 0, 0), folder="101NCZ_F")    # rollover
    a1, a2 = make_zf_cards(tmp_path / "camA", [a], "raw_jpg", junk=False)
    b1, b2 = make_zf_cards(tmp_path / "camB", [b], "raw_jpg", junk=False)
    run_import([a1, a2], dest, name="Trip")
    run_import([b1], dest, names={date(2026, 1, 24): TRIP})
    assert files_under(dest / TRIP) == {"RAW/DSC_0001.NEF", "JPG/DSC_0001.JPG",
                                        "RAW/DSC_0001-2.NEF"}
    for rel in ("RAW/DSC_0001.NEF", "JPG/DSC_0001.JPG"):    # the user culls shot A
        (dest / TRIP / rel).unlink()
    if not history:
        forget_history()
    plan = make_plan([b2], dest, names={date(2026, 1, 24): TRIP})
    assert copies(plan) == {"DSC_0001.JPG": "JPG/DSC_0001-2.JPG"}
    assert not plan.warnings or not any("partner" in w for w in plan.warnings)


# ---- 9: "already imported" needs the recorded library file, not just its name --------------


@pytest.mark.parametrize("same_size", [False, True])
def test_culled_file_whose_name_was_reused_is_not_imported(tmp_path, dest, same_size):
    a = D(2026, 1, 23, 10, 0, 0)
    b = D(2026, 1, 24, 10, 0, 0)
    card = FakeCard(tmp_path / "CARD")
    size = 100_000 if same_size else None       # e.g. uncompressed raws all have one size
    card.photo("DSC_0001.NEF", a, size=size, seed="A")
    card.photo("DSC_0001.NEF", b, folder="101NCZ_F", size=size, seed="B")      # rollover
    run_import([card.root], dest, name="Trip", days=[date(2026, 1, 23)])
    (dest / TRIP / "RAW/DSC_0001.NEF").unlink()                                 # cull A
    run_import([card.root], dest, names={date(2026, 1, 24): TRIP}, days=[date(2026, 1, 24)])
    assert files_under(dest / TRIP) == {"RAW/DSC_0001.NEF"}                    # now shot B

    names = {date(2026, 1, 23): TRIP, date(2026, 1, 24): TRIP}
    plan = make_plan([card.root], dest, names=names)
    by_folder = {i.src.rel.parent.name: i for i in plan.items}
    assert by_folder["100NCZ_F"].action is Action.SKIP_CULLED
    assert by_folder["101NCZ_F"].action is Action.SKIP_IMPORTED
    again = make_plan([card.root], dest, names=names, opts=PlanOptions(reimport_culled=True))
    assert copies(again) == {"DSC_0001.NEF": "RAW/DSC_0001-2.NEF"}


def test_imported_file_with_a_changed_file_time_still_counts(tmp_path, dest):
    """A library copy whose file time changed (synced, touched) is still the same photo."""
    s1, s2 = make_zf_cards(tmp_path / "cards", [FakeShot("DSC_0001", D(2026, 1, 23, 10))],
                           "raw_jpg", junk=False)
    run_import([s1, s2], dest, name="Walk")
    lib = dest / WALK / "RAW/DSC_0001.NEF"
    os.utime(lib, (lib.stat().st_atime, lib.stat().st_mtime + 86_400))
    plan = make_plan([s1, s2], dest, name="Walk")
    assert {i.action for i in plan.items} == {Action.SKIP_IMPORTED}


# ---- 10: --delete-originals also covers days imported in an earlier run ---------------------


def two_day_cards(tmp_path):
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 10)), FakeShot("DSC_0002", D(2026, 1, 24, 11))]
    return make_zf_cards(tmp_path / "cards", shots, "raw_jpg", junk=False)


def test_delete_originals_when_nothing_is_new(tmp_path, dest, capsys):
    s1, s2 = two_day_cards(tmp_path)
    assert run_cli("import", s1, s2, "--dest", dest, "--name", "Trip", "--yes") == 0
    capsys.readouterr()
    assert run_cli("import", s1, s2, "--dest", dest, "--yes", "--delete-originals") == 0
    out = capsys.readouterr().out
    assert "Deleted from cards: 4 files" in out
    assert files_under(s1) == files_under(s2) == set()
    assert len(files_under(dest)) == 4


def test_delete_originals_covers_days_imported_before(tmp_path, dest, capsys):
    s1, s2 = two_day_cards(tmp_path)
    assert run_cli("import", s1, s2, "--dest", dest, "--name", "Trip", "--yes",
                   "--days", "2026-01-23") == 0
    capsys.readouterr()
    assert run_cli("import", s1, s2, "--dest", dest, "--name", "Trip", "--yes",
                   "--delete-originals") == 0
    out = capsys.readouterr().out
    assert "Deleted from cards: 4 files" in out and "its day was not imported" not in out
    assert files_under(s1) == files_under(s2) == set()
    assert files_under(dest) == {
        "2026.01.23 - Trip/RAW/DSC_0001.NEF", "2026.01.23 - Trip/JPG/DSC_0001.JPG",
        "2026.01.24 - Trip/RAW/DSC_0002.NEF", "2026.01.24 - Trip/JPG/DSC_0002.JPG"}


def test_nothing_new_without_delete_still_stops_early(tmp_path, dest, capsys):
    s1, s2 = two_day_cards(tmp_path)
    assert run_cli("import", s1, s2, "--dest", dest, "--name", "Trip", "--yes") == 0
    capsys.readouterr()
    assert run_cli("import", s1, s2, "--dest", dest, "--yes") == 0
    assert "Nothing new to import." in capsys.readouterr().out
    assert len(files_under(s1)) == len(files_under(s2)) == 2


def test_standalone_days_are_never_joined_or_merged(tmp_path):
    from raw_jpg_import.grouping import DayChoice, resolve_targets

    d1, d2, d3 = date(2026, 1, 23), date(2026, 1, 24), date(2026, 1, 25)
    choices = [DayChoice(d1, text="Trip"), DayChoice(d2, text="Mid", standalone=True),
               DayChoice(d3, join_previous=True)]
    for merge_all in (False, True):
        targets, _ = resolve_targets(choices, tmp_path, [], merge_all=merge_all)
        assert targets[d1].folder == targets[d3].folder == "2026.01.23 - Trip"
        assert targets[d2].folder == "2026.01.24 - Mid"


# ---- 11: how a day gets its folder follows the README's order --------------------------------


def three_day_cards(tmp_path):
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 10)), FakeShot("DSC_0002", D(2026, 1, 24, 11)),
             FakeShot("DSC_0003", D(2026, 1, 25, 11))]
    return make_zf_cards(tmp_path / "cards", shots, "raw_jpg", junk=False)


def planned_targets(capsys, *argv: object) -> dict[str, list[str]]:
    code = run_cli("import", *argv, "--dry-run", "--json")
    out, err = capsys.readouterr()
    assert code == 0, err
    return {t["folder"]: t["days"] for t in json.loads(out)["plan"]["targets"]}


def test_date_name_beats_merge(tmp_path, dest, capsys):
    s1, s2 = three_day_cards(tmp_path)
    assert planned_targets(capsys, s1, s2, "--dest", dest, "--merge", "--yes",
                           "--name", "2026-01-23=Trip", "--name", "2026-01-24=Party") == {
        "2026.01.23 - Trip": ["2026.01.23"],
        "2026.01.24 - Party": ["2026.01.24", "2026.01.25"]}


def test_existing_folder_beats_the_generic_name(tmp_path, dest, capsys):
    s1, s2 = three_day_cards(tmp_path)
    (dest / "2026.01.25 - Old Town").mkdir()
    assert planned_targets(capsys, s1, s2, "--dest", dest, "--name", "Beach") == {
        "2026.01.23 - Beach": ["2026.01.23"], "2026.01.24 - Beach": ["2026.01.24"],
        "2026.01.25 - Old Town": ["2026.01.25"]}


def test_join_and_a_name_for_the_same_day_is_a_usage_error(tmp_path, dest, capsys):
    s1, s2 = three_day_cards(tmp_path)
    assert run_cli("import", s1, s2, "--dest", dest, "--join", "2026-01-24",
                   "--name", "2026-01-24=Party", "--dry-run") == 2
    assert "contradict" in capsys.readouterr().err


# ---- 12: an existing folder is reused even when cleaning would change its name -----------


@pytest.mark.parametrize("existing, text", [
    ("2026.01.23 - Rock--Paper", "Rock--Paper"),          # the name part, as the GUI prefills
    ("2026.01.23 - Rock--Paper", "2026.01.23 - Rock--Paper"),
    ("2026.01.23 - Trip  2", "Trip  2"),
    ("2026.01.23 - Trip  2", "Trip 2"),                   # cleaned text = cleaned folder name
])
def test_existing_folder_with_an_uncleaned_name_is_reused(tmp_path, existing, text):
    from raw_jpg_import.grouping import DayChoice, find_existing_events, resolve_targets

    (tmp_path / existing).mkdir()
    day = date(2026, 1, 23)
    targets, _ = resolve_targets([DayChoice(day, text=text)], tmp_path,
                                 find_existing_events(tmp_path))
    assert (targets[day].folder, targets[day].exists) == (existing, True)


def test_cleaned_folder_that_exists_wins_over_a_similar_one(tmp_path):
    from raw_jpg_import.grouping import DayChoice, find_existing_events, resolve_targets

    for name in ("2026.01.23 - Trip  2", "2026.01.23 - Trip 2"):
        (tmp_path / name).mkdir()
    day = date(2026, 1, 23)
    targets, _ = resolve_targets([DayChoice(day, text="Trip 2")], tmp_path,
                                 find_existing_events(tmp_path))
    assert targets[day].folder == "2026.01.23 - Trip 2"


def test_cli_name_reuses_the_existing_folder(tmp_path, dest, capsys):
    s1, s2 = make_zf_cards(tmp_path / "cards", [FakeShot("DSC_0001", D(2026, 1, 23, 18))],
                           "raw_jpg", junk=False)
    (dest / "2026.01.23 - Rock--Paper").mkdir()
    (dest / "2026.01.23 - Other").mkdir()
    assert planned_targets(capsys, s1, s2, "--dest", dest, "--name", "Rock--Paper") == {
        "2026.01.23 - Rock--Paper": ["2026.01.23"]}


def test_long_full_folder_name_keeps_its_name_part(tmp_path):
    from raw_jpg_import.grouping import DayChoice, resolve_targets

    name = "N" * 80
    day = date(2026, 1, 24)
    targets, _ = resolve_targets([DayChoice(day, text=f"2026.01.23 - {name}")], tmp_path, [])
    assert targets[day].folder == f"2026.01.23 - {name}"


# ---- 13: JPEG + JPEG slot mode imported one card at a time --------------------------------


@pytest.mark.parametrize("order", ["slot1 first", "slot2 first", "together"])
def test_jpeg_jpeg_cards_one_at_a_time(tmp_path, dest, order):
    s1, s2 = make_zf_cards(tmp_path / "cards", [FakeShot("DSC_0001", D(2026, 1, 23, 10))],
                           "jpg_jpg", junk=False)
    runs = {"slot1 first": [[s1], [s2]], "slot2 first": [[s2], [s1]], "together": [[s1, s2]]}
    for cards in runs[order]:
        run_import(cards, dest, name="X")
    assert files_under(dest / "2026.01.23 - X") == {
        "RAW/DSC_0001.NEF", "JPG/DSC_0001.JPG", "JPG/DSC_0001-v2.JPG"}
    again = make_plan([s1, s2], dest, name="X")
    assert not again.to_copy
    assert len(again.items) == 3


def test_a_different_photo_with_the_same_name_is_still_a_clash(tmp_path, dest):
    """Only another version of the *same* shot becomes -v2; another shot still gets -2."""
    a = FakeCard(tmp_path / "A")
    a.photo("DSC_0001.JPG", D(2026, 1, 23, 10), seed="a")
    b = FakeCard(tmp_path / "B")
    b.photo("DSC_0001.JPG", D(2026, 1, 23, 11), seed="b")
    run_import([a.root], dest, name="X")
    assert copies(make_plan([b.root], dest, name="X")) == {"DSC_0001.JPG": "JPG/DSC_0001-2.JPG"}


def test_copy_edited_in_place_still_counts_as_imported(tmp_path, dest):
    """A library JPG that an editor rewrote (metadata written into the file) is not 'culled'."""
    s1, s2 = make_zf_cards(tmp_path / "cards", [FakeShot("DSC_0001", D(2026, 1, 23, 10))],
                           "raw_jpg", junk=False)
    run_import([s1, s2], dest, name="Walk")
    jpg = dest / WALK / "JPG/DSC_0001.JPG"
    jpg.write_bytes(jpg.read_bytes() + b"<xmp keywords/>")
    plan = make_plan([s1, s2], dest, name="Walk")
    assert {i.action for i in plan.items} == {Action.SKIP_IMPORTED}


# ---- 14: a daylight-saving change does not make fallback-dated files new again ------------


def shift_mtime(path, seconds: int) -> None:
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


@pytest.fixture
def summer_card(tmp_path):
    card = FakeCard(tmp_path / "CARD", "SONY")
    start = D(2026, 7, 1, 14, 0, 0)
    clip = write_file(card.path("PRIVATE/M4ROOT/CLIP/C0001.MP4"),
                      make_mov(start, 120, 12.0, nctg=False, brand=b"mp42", name="C0001.MP4"),
                      video_mtime(start, 12.0))
    photo = card.photo("DSC00001.JPG", D(2026, 7, 1, 14, 5, 0), folder="100MSDCF")
    return card, clip, photo


@pytest.mark.parametrize("history", [True, False])
@pytest.mark.parametrize("shift", [3600, -3600])
def test_dst_shift_on_the_card_copies_nothing(dest, summer_card, history, shift):
    card, clip, photo = summer_card
    report = run_import([card.root], dest, name="Summer")
    assert len(report.plan.to_copy) == 2
    assert {i.capture.source for i in report.plan.items} == {"mvhd+mtime", "exif"}
    if not history:
        forget_history()
    for p in (clip, photo):
        shift_mtime(p, shift)
    metadata.clear_cache()
    plan = make_plan([card.root], dest, name="Summer")
    assert plan.to_copy == []


def test_one_hour_apart_but_other_bytes_is_a_different_file(dest, summer_card):
    card, clip, _ = summer_card
    run_import([card.root], dest, name="Summer")
    lib = next((dest / "2026.07.01 - Summer" / "MOV").iterdir())
    data = bytearray(lib.read_bytes())
    data[len(data) // 2] ^= 0xFF                 # same size and time, other content
    st = lib.stat()
    lib.write_bytes(bytes(data))
    os.utime(lib, ns=(st.st_atime_ns, st.st_mtime_ns))
    forget_history()
    shift_mtime(clip, 3600)
    metadata.clear_cache()
    assert copies(make_plan([card.root], dest, name="Summer")) == {"C0001.MP4": "MOV/C0001-2.MP4"}


# ---- 22: typos and unknown dates in --name DATE=NAME and --join are usage errors ----------


@pytest.mark.parametrize("extra, message", [
    (["--name", "2026-01-32=Party"], "out of range"),
    (["--name", "2026.13.01=Party"], "2026.13.01=Party"),
    (["--name", "2026-01-25=Party", "--yes"], "no photos on 2026-01-25"),
    (["--name", "2026-01-23=Party", "--join", "2026-01-25", "--yes"],
     "--join: no photos on 2026-01-25"),
    (["--name", "2026-01-23=A", "--name", "2026-01-23=B"], "two names for 2026-01-23"),
])
def test_bad_dates_in_names_and_joins(tmp_path, dest, capsys, extra, message):
    s1, s2 = two_day_cards(tmp_path)
    assert run_cli("import", s1, s2, "--dest", dest, "--dry-run", *extra) == 2
    assert message in capsys.readouterr().err
    assert list(dest.iterdir()) == []


def test_names_with_an_equals_sign_are_still_names(tmp_path, dest, capsys):
    s1, s2 = two_day_cards(tmp_path)
    assert planned_targets(capsys, s1, s2, "--dest", dest, "--name", "A=B") == {
        "2026.01.23 - A=B": ["2026.01.23"], "2026.01.24 - A=B": ["2026.01.24"]}


# ---- 23: sidecars are dated by their photo, so they never count as "no capture date" -------


def test_sidecars_do_not_count_as_files_without_a_date(tmp_path, dest):
    when = D(2026, 1, 23, 18, 0, 0)
    s1, s2 = make_zf_cards(tmp_path / "cards", [FakeShot("DSC_0001", when),
                                                FakeShot("DSC_0002", when + timedelta(minutes=1))],
                           "raw_jpg", junk=False)
    FakeCard(s1).file("DCIM/100NCZ_F/DSC_0001.WAV", b"RIFF....WAVEfmt " + bytes(200), when)
    FakeCard(s1).file("DCIM/100NCZ_F/DSC_0002.NEF.xmp", b"<x:xmpmeta/>", when)
    plan = make_plan([s1, s2], dest, name="Walk")
    assert len(plan.to_copy) == 6
    assert not any("no capture date" in w for w in plan.warnings)


def test_a_file_without_a_date_is_still_counted(tmp_path, dest):
    card = FakeCard(tmp_path / "CARD")
    card.file("DCIM/100NCZ_F/DSC_0001.JPG", b"no exif here" * 100, D(2026, 1, 23, 18))
    card.file("DCIM/100NCZ_F/DSC_0001.WAV", b"RIFF....WAVEfmt " + bytes(200), D(2026, 1, 23, 18))
    plan = make_plan([card.root], dest, name="Walk")
    assert "1 files have no capture date; using the file time" in plan.warnings


# ---- 24: copied unknown files from days without photos go with the nearest day -------------


@pytest.fixture
def card_with_old_unknowns(tmp_path):
    card = FakeCard(tmp_path / "CARD")
    card.photo("DSC_0001.NEF", D(2026, 1, 23, 10))
    card.photo("DSC_0002.NEF", D(2026, 1, 26, 10))
    card.file("VRAM0.BMP", b"BM" + bytes(300), D(2025, 9, 24, 0, 30))     # no photos that day
    card.file("DCIM/100NCZ_F/NOTES.XYZ", b"x" * 50, D(2026, 1, 25, 12))   # nor that day
    return card


def test_copy_unknown_from_days_without_photos(dest, card_with_old_unknowns):
    card = card_with_old_unknowns
    plan = make_plan([card.root], dest, name="Trip", opts=PlanOptions(copy_unknown=True))
    assert copies(plan) == {"DSC_0001.NEF": "RAW/DSC_0001.NEF", "DSC_0002.NEF": "RAW/DSC_0002.NEF",
                            "VRAM0.BMP": "OTHER/VRAM0.BMP", "NOTES.XYZ": "OTHER/NOTES.XYZ"}
    by_name = {i.src.name: i for i in plan.items}
    assert by_name["VRAM0.BMP"].target.folder == "2026.01.23 - Trip"
    assert by_name["NOTES.XYZ"].target.folder == "2026.01.26 - Trip"      # 1 day, not 2
    assert by_name["NOTES.XYZ"].day == date(2026, 1, 26)
    assert "1 files are from days without photos and go into 2026.01.23 - Trip" in plan.warnings


def test_unknown_files_of_an_unticked_day_stay_unselected(dest, card_with_old_unknowns):
    card = card_with_old_unknowns
    card.file("DCIM/100NCZ_F/LATE.XYZ", b"y" * 50, D(2026, 1, 26, 12))
    plan = make_plan([card.root], dest, name="Trip", days=[date(2026, 1, 23)],
                     opts=PlanOptions(copy_unknown=True))
    by_name = {i.src.name: i for i in plan.items}
    assert by_name["LATE.XYZ"].action is Action.SKIP_DESELECTED
    assert by_name["NOTES.XYZ"].target.folder == "2026.01.23 - Trip"


def test_cli_copy_unknown_takes_them_even_when_nothing_else_is_new(dest, capsys,
                                                                   card_with_old_unknowns):
    card = card_with_old_unknowns
    assert run_cli("import", card.root, "--dest", dest, "--name", "Trip", "--yes") == 0
    assert run_cli("import", card.root, "--dest", dest, "--yes", "--copy-unknown") == 0
    assert {"2026.01.23 - Trip/OTHER/VRAM0.BMP", "2026.01.26 - Trip/OTHER/NOTES.XYZ"} <= \
        files_under(dest)


# ---- 29 / 30: the CLI does what the README says ---------------------------------------------


@pytest.mark.parametrize("argv", [
    ["-q", "import"], ["import", "-q"], ["import", "--quiet"], ["-v", "scan"], ["scan", "-v"],
])
def test_verbosity_flags_before_or_after_the_command(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.quiet == ("-q" in argv or "--quiet" in argv)
    assert args.verbose == ("-v" in argv)


def test_quiet_after_the_command_runs_quietly(tmp_path, dest, capsys):
    s1, s2 = two_day_cards(tmp_path)
    assert run_cli("import", s1, s2, "--dest", dest, "--name", "X", "--dry-run", "-q") == 0
    assert capsys.readouterr().out == ""


def test_scan_uses_the_last_destination(tmp_path, dest, capsys):
    s1, s2 = two_day_cards(tmp_path)
    assert run_cli("import", s1, s2, "--dest", dest, "--name", "Walk", "--yes",
                   "--days", "2026-01-23") == 0
    capsys.readouterr()
    assert run_cli("scan", s1, s2, "--json") == 0
    days = {d["date"]: d for d in json.loads(capsys.readouterr().out)["days"]}
    assert days["2026-01-23"]["new"] == 0 and days["2026-01-24"]["new"] == 2
    assert days["2026-01-23"]["suggestions"] == ["2026.01.23 - Walk"]


# ---- 31: macOS library roots compare without case, like the file system ---------------------


def test_root_key_ignores_case_on_macos(monkeypatch, tmp_path):
    a, b = tmp_path / "Bibliothek Straße", tmp_path / "BIBLIOTHEK STRASSE"
    assert root_key(a) != root_key(b)              # Windows normcase / Linux: not folded
    monkeypatch.setattr(ledger_mod, "_casefold_paths", lambda: True)
    assert root_key(a) == root_key(b) == root_key(root_key(a))


def test_macos_history_is_rekeyed_once(monkeypatch, tmp_path):
    db = tmp_path / "history.sqlite3"
    lib = tmp_path / "Bibliothek Straße"
    with Ledger.open(db) as led:                    # written before keys were folded
        led.record(Row(id=None, session="s", kind="copied", dest_root=str(lib), event="E",
                       dest_rel="RAW/A.NEF", orig_key="a.nef", size=1, mtime_ns=0,
                       capture="2026-01-23T10:00:00", capture_exact=True, subsec=None,
                       shot_stem_key="a", sha256=None, card_label="C", card_serial=None,
                       src_rel="DCIM/A.NEF"))
    monkeypatch.setattr(ledger_mod, "_casefold_paths", lambda: True)
    with Ledger.open(db) as led:
        typed_otherwise = tmp_path / "BIBLIOTHEK STRASSE"
        assert [r.dest_rel for r in led.rows_for_event(typed_otherwise, "E")] == ["RAW/A.NEF"]
        assert led.days_by_event(typed_otherwise) == {"E": {date(2026, 1, 23)}}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key = 'root_keys'").fetchone() == \
            ("casefold",)


# ---- 33: a folder on a card protects the whole card --------------------------------------


@pytest.fixture
def card_in_reader(tmp_path, monkeypatch):
    r"""A fake card that volume detection reports as a card (stands for F:\)."""
    card = FakeCard(tmp_path / "card", "NIKON Z F")
    card.photo("DSC_0001.NEF", D(2026, 1, 23, 10), subsec="11")

    def volume_for_path(path):
        if Path(path).resolve().is_relative_to(card.root.resolve()):
            return Volume(card.root, "NIKON Z F", "1234-ABCD", 0, 0, True)
        return None

    monkeypatch.setattr(volumes, "volume_for_path", volume_for_path)
    return card


def test_destination_elsewhere_on_the_card_of_a_folder_source_is_blocked(card_in_reader):
    card = card_in_reader
    backup = card.root / "Backup"
    backup.mkdir()
    before = card.state()
    plan = make_plan([card.root / "DCIM"], backup, name="Trip", probe_write=True)
    assert [s.kind for s in plan.sources] == ["folder"]
    assert any("is on the same card as the source" in p and "(NIKON Z F)" in p
               for p in plan.problems)
    assert card.state() == before                    # not even the write probe
    assert list(backup.iterdir()) == []


def test_destination_off_the_card_is_fine(card_in_reader, dest):
    plan = make_plan([card_in_reader.root / "DCIM"], dest, name="Trip")
    assert plan.problems == []


# ---- 34: the destination is made absolute before paths are measured -----------------------


def test_relative_destination_is_planned_absolute(tmp_path, monkeypatch):
    card = FakeCard(tmp_path / "card")
    card.photo("DSC_0001.NEF", D(2026, 1, 23, 10))
    (tmp_path / "lib").mkdir()
    monkeypatch.chdir(tmp_path)
    plan = make_plan([card.root], Path("lib"), name="Trip")
    assert plan.dest_root == tmp_path / "lib"
    assert all(i.dest.is_absolute() for i in plan.to_copy)


@pytest.mark.skipif(sys.platform != "win32", reason="the 260-character limit is Windows'")
def test_relative_destination_too_long_on_disk_is_blocked(tmp_path, monkeypatch):
    card = FakeCard(tmp_path / "card")
    card.photo("DSC_0001.NEF", D(2026, 1, 23, 10))
    deep = tmp_path / "w" / ("d" * 40) / ("e" * 40) / ("f" * 20)
    (deep / "lib").mkdir(parents=True)
    monkeypatch.chdir(deep)
    name = "E" * 60
    rel = Path("lib") / f"2026.01.23 - {name}" / "RAW" / ".DSC_0001.NEF.rji-000000.part"
    assert len(str(rel)) <= 259 < len(str(deep / rel))
    plan = make_plan([card.root], Path("lib"), name=name)
    assert any(p.startswith("Path too long for Windows") for p in plan.problems)
