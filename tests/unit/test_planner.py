"""Planner: actions, destination names, ledger and disk checks, blocking problems.

Plans are built from fake cards on disk through ``engine.plan``, exactly as the GUI and the
CLI build them. Planning must never write anything.
"""

from __future__ import annotations

import os
import shutil
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes.card import (
    DAY1,
    DAY2,
    EVENT,
    EVENT_NAME,
    FakeCard,
    FakeShot,
    files_under,
    make_plan,
    make_zf_cards,
    no_real_cards,
    run_import,
    zf_cards,
)
from fakes.files import write_file
from fakes.jpeg import make_jpeg

from raw_jpg_import import engine, planner
from raw_jpg_import.planner import Action, ImportPlan, PlanItem, PlanOptions

D = datetime
PAIRS = [
    FakeShot("DSC_0001", D(2026, 1, 23, 23, 2, 10)),
    FakeShot("DSC_0002", D(2026, 1, 23, 23, 15, 42)),
    FakeShot("DSC_0003", D(2026, 1, 23, 23, 31, 5)),
]


@pytest.fixture(autouse=True)
def _no_real_cards(monkeypatch):
    no_real_cards(monkeypatch)


# Planning only reads the cards, so they are built once per module (scans then hit the
# metadata cache). Tests here must never modify them.


@pytest.fixture(scope="module")
def zf(tmp_path_factory):
    return zf_cards(tmp_path_factory.mktemp("cards"))


@pytest.fixture(scope="module")
def pairs(tmp_path_factory):
    """Three NEF+JPG shots on two cards, nothing else."""
    return make_zf_cards(tmp_path_factory.mktemp("pairs"), PAIRS, "raw_jpg", junk=False)


@pytest.fixture(scope="module")
def rollover(tmp_path_factory):
    """DSC_0001 at 18:00 and again (counter rollover, 101NCZ_F) at 23:40, plus DSC_0002."""
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 18, 0, 0)),
             FakeShot("DSC_0002", D(2026, 1, 23, 23, 2, 10)),
             FakeShot("DSC_0001", D(2026, 1, 23, 23, 40, 0), folder="101NCZ_F")]
    return make_zf_cards(tmp_path_factory.mktemp("rollover"), shots, "raw_jpg", junk=False)


@pytest.fixture(scope="module", autouse=True)
def _cards_unchanged(zf, pairs, rollover):
    def state():
        return zf.state(), [FakeCard(r).state() for r in (*pairs, *rollover)]

    before = state()
    yield
    assert state() == before


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "Photos lib"
    d.mkdir()
    return d


def dests(plan: ImportPlan) -> dict[str, tuple[str, str | None]]:
    """``{"SLOT1:100NCZ_F/DSC_0001.NEF": ("copy", "RAW/DSC_0001.NEF"), ...}``."""
    out = {}
    for i in plan.items:
        rel = i.src.rel.as_posix().removeprefix("DCIM/")
        out[f"{i.src.source.label}:{rel}"] = (i.action.name.lower(),
                                              i.dest_rel.as_posix() if i.dest_rel else None)
    return out


def item(plan: ImportPlan, name: str, label: str | None = None) -> PlanItem:
    """The single item whose card path ends with ``name`` (optionally on card ``label``)."""
    hits = [i for i in plan.items if i.src.rel.as_posix().endswith(name)
            and (label is None or i.src.source.label == label)]
    assert len(hits) == 1, [str(h.src.path) for h in hits]
    return hits[0]


def lib(dest: Path, rel: str) -> Path:
    return dest / EVENT / rel


def stable_json(plan: ImportPlan) -> dict:
    data = plan.to_json()
    data.pop("session")
    data.pop("free_bytes")
    return data


# ---- folders and names ------------------------------------------------------------------------


def test_raw_jpg_only_creates_raw_and_jpg(pairs, dest):
    plan = make_plan(pairs, dest)
    assert not plan.problems
    assert [t.target.folder for t in plan.targets] == [EVENT]
    assert list(plan.targets[0].per_folder) == ["RAW", "JPG"]
    assert dests(plan) == {
        "SLOT1:100NCZ_F/DSC_0001.NEF": ("copy", "RAW/DSC_0001.NEF"),
        "SLOT2:100NCZ_F/DSC_0001.JPG": ("copy", "JPG/DSC_0001.JPG"),
        "SLOT1:100NCZ_F/DSC_0002.NEF": ("copy", "RAW/DSC_0002.NEF"),
        "SLOT2:100NCZ_F/DSC_0002.JPG": ("copy", "JPG/DSC_0002.JPG"),
        "SLOT1:100NCZ_F/DSC_0003.NEF": ("copy", "RAW/DSC_0003.NEF"),
        "SLOT2:100NCZ_F/DSC_0003.JPG": ("copy", "JPG/DSC_0003.JPG"),
    }
    assert files_under(dest) == set()


def test_video_adds_mov_folder(zf, dest):
    plan = make_plan(zf.roots, dest)
    assert list(plan.targets[0].per_folder) == ["RAW", "JPG", "MOV"]
    assert plan.targets[0].per_folder["MOV"][0] == 1
    assert item(plan, "DSC_0010.MOV").dest_rel.as_posix() == "MOV/DSC_0010.MOV"


def test_sunset_walk_plan(zf, dest):
    plan = make_plan(zf.roots, dest)
    d = dests(plan)
    assert d["SLOT1:101NCZ_F/DSC_0001.NEF"] == ("copy", "RAW/DSC_0001-2.NEF")
    assert d["SLOT2:101NCZ_F/DSC_0001.JPG"] == ("copy", "JPG/DSC_0001-2.JPG")
    assert d["SLOT1:100NCZ_F/_DSC0005.NEF"] == ("copy", "RAW/_DSC0005.NEF")
    assert d["SLOT2:100NCZ_F/_DSC0005.JPG"] == ("copy", "JPG/_DSC0005.JPG")
    assert d["SLOT1:100NCZ_F/NOTES.XYZ"] == ("unknown", None)
    assert plan.renamed == 2
    assert plan.counts() == {Action.COPY: 21, Action.UNKNOWN: 1}
    assert any(".xyz x1" in w for w in plan.warnings)
    # The walk past midnight stays on the 23rd with the 01:00 day start.
    assert plan.targets[0].target.days == frozenset({DAY1})
    assert {i.src.name for i in plan.items} >= {"DSC_0007.NEF", "DSC_0009.JPG"}


def test_jpg_only_clash_renames_nef_too(pairs, dest):
    foreign = make_jpeg(D(2025, 7, 1, 12, 0, 0), name="someone else's photo")
    write_file(lib(dest, "JPG/DSC_0002.JPG"), foreign, D(2025, 7, 1, 12, 0, 0))
    plan = make_plan(pairs, dest)
    d = dests(plan)
    assert d["SLOT1:100NCZ_F/DSC_0002.NEF"] == ("copy", "RAW/DSC_0002-2.NEF")
    assert d["SLOT2:100NCZ_F/DSC_0002.JPG"] == ("copy", "JPG/DSC_0002-2.JPG")
    assert d["SLOT1:100NCZ_F/DSC_0001.NEF"] == ("copy", "RAW/DSC_0001.NEF")
    assert plan.targets[0].target.exists
    assert plan.renamed == 2


def test_existing_stem_of_other_shot_in_another_subfolder_blocks_it(pairs, dest):
    """A MOV/DSC_0003.MOV of a different moment owns the stem for every subfolder."""
    clip = make_jpeg(D(2025, 1, 1), name="not a photo of this shot")
    write_file(lib(dest, "MOV/DSC_0003.MOV"), clip, D(2025, 1, 1, 10, 0, 0))
    plan = make_plan(pairs, dest)
    assert item(plan, "DSC_0003.NEF").dest_rel.as_posix() == "RAW/DSC_0003-2.NEF"
    assert item(plan, "DSC_0003.JPG").dest_rel.as_posix() == "JPG/DSC_0003-2.JPG"


@pytest.mark.parametrize("identical", [False, True])
def test_lowercase_nef_collides_with_existing_uppercase(tmp_path, dest, identical):
    card = FakeCard(tmp_path / "card")
    src = card.photo("dsc_0002.nef", D(2026, 1, 23, 23, 15, 42), subsec="10")
    target = lib(dest, "RAW/DSC_0002.NEF")
    if identical:
        target.parent.mkdir(parents=True)
        shutil.copy2(src, target)
    else:
        write_file(target, b"another raw file", D(2026, 1, 1))
    plan = make_plan([card.root], dest)
    it = item(plan, "dsc_0002.nef")
    if identical:
        assert it.action is Action.SKIP_EXISTS
        assert it.existing == target
    else:
        assert it.action is Action.COPY
        assert it.dest_rel.as_posix() == "RAW/dsc_0002-2.nef"     # original case kept


def test_identical_file_already_in_folder_is_skipped_and_adopted(pairs, dest):
    nef = Path(pairs[0]) / "DCIM/100NCZ_F/DSC_0002.NEF"
    (dest / EVENT / "RAW").mkdir(parents=True)
    shutil.copy2(nef, lib(dest, "RAW/DSC_0002.NEF"))
    plan = make_plan(pairs, dest)
    it = item(plan, "DSC_0002.NEF")
    assert it.action is Action.SKIP_EXISTS
    assert it.existing == lib(dest, "RAW/DSC_0002.NEF")
    assert item(plan, "DSC_0002.JPG").dest_rel.as_posix() == "JPG/DSC_0002.JPG"

    report = engine.execute(plan)
    res = report.results[it.id]
    assert res.outcome.name == "ALREADY_THERE" and res.ledger_id is not None
    from raw_jpg_import.ledger import Ledger

    with Ledger.open() as led:
        row = led.get(res.ledger_id)
    assert row is not None and row.kind == "adopted" and row.dest_rel == "RAW/DSC_0002.NEF"
    again = make_plan(pairs, dest)
    assert item(again, "DSC_0002.NEF").action is Action.SKIP_IMPORTED
    assert again.to_copy == []


def test_same_name_and_size_but_other_time_is_a_different_file(pairs, dest):
    nef = Path(pairs[0]) / "DCIM/100NCZ_F/DSC_0002.NEF"
    other = make_jpeg(D(2024, 5, 5), size=nef.stat().st_size, name="same size")
    write_file(lib(dest, "RAW/DSC_0002.NEF"), other, D(2024, 5, 5, 9, 0, 0))
    plan = make_plan(pairs, dest)
    assert item(plan, "DSC_0002.NEF").dest_rel.as_posix() == "RAW/DSC_0002-2.NEF"


def test_size_differs_other_capture_renames(pairs, dest):
    nef = Path(pairs[0]) / "DCIM/100NCZ_F/DSC_0002.NEF"
    other = make_jpeg(D(2024, 5, 5), size=nef.stat().st_size + 100, name="other shot")
    write_file(lib(dest, "RAW/DSC_0002.NEF"), other, D(2024, 5, 5, 9, 0, 0))
    plan = make_plan(pairs, dest)
    assert item(plan, "DSC_0002.NEF").dest_rel.as_posix() == "RAW/DSC_0002-2.NEF"
    assert item(plan, "DSC_0002.JPG").dest_rel.as_posix() == "JPG/DSC_0002-2.JPG"
    assert item(plan, "DSC_0002.NEF").renamed


def test_size_differs_same_capture_is_another_version(pairs, dest):
    """Another version of the same shot holds the name (edited in place, or JPEG + JPEG
    cards imported one at a time): the card file is kept as -v2 and the shot keeps its stem."""
    nef = Path(pairs[0]) / "DCIM/100NCZ_F/DSC_0002.NEF"
    write_file(lib(dest, "RAW/DSC_0002.NEF"), nef.read_bytes() + b"edited",
               D(2026, 1, 23, 23, 15, 42))
    plan = make_plan(pairs, dest)
    it = item(plan, "DSC_0002.NEF")
    assert (it.action, it.dest_rel.as_posix()) == (Action.COPY, "RAW/DSC_0002-v2.NEF")
    assert it.variant == 2 and not it.renamed
    assert item(plan, "DSC_0002.JPG").dest_rel.as_posix() == "JPG/DSC_0002.JPG"


# ---- the import history ------------------------------------------------------------------------


def test_culled_file_is_skipped_or_reimported(rollover, dest):
    run_import(rollover, dest)
    lib(dest, "RAW/DSC_0002.NEF").unlink()
    lib(dest, "RAW/DSC_0001-2.NEF").unlink()
    plan = make_plan(rollover, dest)
    culled = item(plan, "DSC_0002.NEF")
    assert culled.action is Action.SKIP_CULLED
    assert "removed from library" in culled.reason
    assert item(plan, "101NCZ_F/DSC_0001.NEF").action is Action.SKIP_CULLED
    assert item(plan, "DSC_0002.JPG").action is Action.SKIP_IMPORTED
    assert plan.to_copy == []
    assert any("removed from the library" in w for w in plan.warnings)

    again = make_plan(rollover, dest, opts=PlanOptions(reimport_culled=True))
    assert {i.dest_rel.as_posix() for i in again.to_copy} == {"RAW/DSC_0002.NEF",
                                                              "RAW/DSC_0001-2.NEF"}


def test_ledger_row_for_another_destination_root_copies(pairs, tmp_path, dest):
    other = tmp_path / "Other library"
    other.mkdir()
    run_import(pairs, other)
    plan = make_plan(pairs, dest)
    assert plan.counts()[Action.COPY] == 6
    assert not any(i.action is Action.SKIP_IMPORTED for i in plan.items)


def test_ledger_points_to_another_event_folder(pairs, dest):
    run_import(pairs, dest)
    plan = make_plan(pairs, dest, name="Renamed Walk")
    imported = [i for i in plan.items if i.action is Action.SKIP_IMPORTED]
    assert len(imported) == 6
    assert all(i.existing is not None and i.existing.is_relative_to(dest / EVENT)
               for i in imported)
    assert all(i.target.folder == EVENT for i in imported)
    assert plan.to_copy == []
    assert any(f"6 files were already imported into {EVENT}" in w for w in plan.warnings)


def test_ledger_ignored_when_the_event_folder_was_renamed(pairs, dest):
    run_import(pairs, dest)
    (dest / EVENT).rename(dest / "2026.01.23 - Renamed")
    plan = make_plan(pairs, dest, name="Renamed")
    # Disk checks: everything is found again in the renamed folder.
    assert plan.counts()[Action.SKIP_EXISTS] == 6
    assert plan.to_copy == []


# ---- several cards -----------------------------------------------------------------------------


def test_backup_mode_copies_once(tmp_path, dest):
    roots = make_zf_cards(tmp_path / "bk", PAIRS, "backup", junk=False)
    plan = make_plan(roots, dest)
    c = plan.counts()
    assert c[Action.COPY] == 6 and c[Action.SKIP_DUPLICATE] == 6
    for dup in (i for i in plan.items if i.action is Action.SKIP_DUPLICATE):
        main = plan.items[dup.dup_of]
        assert dup.src.source.label == "SLOT2" and main.src.source.label == "SLOT1"
        assert main.action is Action.COPY and main.src.name == dup.src.name
        assert dup.dest_rel == main.dest_rel
    assert any("identical on both cards" in w for w in plan.warnings)


def test_jpg_jpg_mode_names_the_smaller_jpg_v2(tmp_path, dest):
    roots = make_zf_cards(tmp_path / "jj", PAIRS[:1], "jpg_jpg", junk=False)
    plan = make_plan(roots, dest)
    assert dests(plan) == {
        "SLOT1:100NCZ_F/DSC_0001.NEF": ("copy", "RAW/DSC_0001.NEF"),
        "SLOT1:100NCZ_F/DSC_0001.JPG": ("copy", "JPG/DSC_0001.JPG"),
        "SLOT2:100NCZ_F/DSC_0001.JPG": ("copy", "JPG/DSC_0001-v2.JPG"),
    }
    assert any("-v2" in w for w in plan.warnings)


def test_raw_heif_mode_puts_hif_next_to_jpgs(tmp_path, dest):
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 23, 2, 10), quality="RAW+HIF")]
    roots = make_zf_cards(tmp_path / "rh", shots, "raw_heif", junk=False)
    plan = make_plan(roots, dest)
    assert dests(plan) == {
        "SLOT1:100NCZ_F/DSC_0001.NEF": ("copy", "RAW/DSC_0001.NEF"),
        "SLOT2:100NCZ_F/DSC_0001.HIF": ("copy", "JPG/DSC_0001.HIF"),
    }
    assert plan.items[0].shot_id == plan.items[1].shot_id


def test_planning_is_deterministic(zf, dest):
    write_file(lib(dest, "JPG/DSC_0003.JPG"), make_jpeg(D(2025, 1, 1), name="x"), D(2025, 1, 1))
    before = files_under(dest)
    a = make_plan(zf.roots, dest)
    b = make_plan(zf.roots, dest)
    assert stable_json(a) == stable_json(b)
    assert a.session_id != b.session_id
    assert files_under(dest) == before          # planning writes nothing


def test_deselected_days(zf, dest):
    from datetime import time

    plan = make_plan(zf.roots, dest, day_start=time(0, 0), days=[DAY1])
    late = [i for i in plan.items if i.capture and i.capture.local.date() == DAY2]
    assert late and all(i.action is Action.SKIP_DESELECTED and i.dest_rel is None for i in late)
    assert not any(i.action is Action.SKIP_DESELECTED for i in plan.items
                   if i.capture and i.capture.local.date() == DAY1)


def test_join_and_names_per_day(zf, dest):
    from datetime import time

    plan = make_plan(zf.roots, dest, day_start=time(0, 0),
                     names={DAY1: EVENT_NAME, DAY2: "After Midnight"})
    assert [t.target.folder for t in plan.targets] == [EVENT, "2026.01.24 - After Midnight"]
    joined = make_plan(zf.roots, dest, day_start=time(0, 0), joins=[DAY2])
    assert [t.target.folder for t in joined.targets] == [EVENT]
    assert joined.targets[0].target.days == frozenset({DAY1, DAY2})


# ---- unknown files and orphan sidecars --------------------------------------------------------


def test_unknown_reported_by_default(zf, dest):
    plan = make_plan(zf.roots, dest)
    it = item(plan, "NOTES.XYZ")
    assert it.action is Action.UNKNOWN and it.dest_rel is None
    assert plan.unknown == {"xyz": 1}


def test_unknown_copy_goes_to_other(zf, dest):
    plan = make_plan(zf.roots, dest, opts=PlanOptions(copy_unknown=True))
    it = item(plan, "NOTES.XYZ")
    assert it.action is Action.COPY and it.dest_rel.as_posix() == "OTHER/NOTES.XYZ"
    assert list(plan.targets[0].per_folder)[-1] == "OTHER"


def test_unknown_copy_name_clash_and_identical(zf, dest):
    write_file(lib(dest, "OTHER/NOTES.XYZ"), b"something else", D(2026, 1, 1))
    plan = make_plan(zf.roots, dest, opts=PlanOptions(copy_unknown=True))
    assert item(plan, "NOTES.XYZ").dest_rel.as_posix() == "OTHER/NOTES-2.XYZ"
    lib(dest, "OTHER/NOTES.XYZ").unlink()
    shutil.copy2(zf.slot1.find("NOTES.XYZ"), lib(dest, "OTHER/NOTES.XYZ"))
    plan = make_plan(zf.roots, dest, opts=PlanOptions(copy_unknown=True))
    assert item(plan, "NOTES.XYZ").action is Action.SKIP_EXISTS


def test_unknown_copy_policy_from_config(zf, dest):
    from raw_jpg_import import appdirs
    from raw_jpg_import.config import load_config

    path = appdirs.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('schema = 1\n[import]\nunknown = "copy"\n', encoding="utf-8")
    cfg = load_config()
    plan = make_plan(zf.roots, dest, cfg=cfg)
    assert item(plan, "NOTES.XYZ").action is Action.COPY
    plan = make_plan(zf.roots, dest, cfg=cfg, opts=PlanOptions(copy_unknown=False))
    assert item(plan, "NOTES.XYZ").action is Action.UNKNOWN


def test_orphan_sidecar(tmp_path, dest):
    card = FakeCard(tmp_path / "card")
    card.photo("DSC_0001.NEF", D(2026, 1, 23, 23, 2, 10))
    card.file("DCIM/100NCZ_F/DSC_0001.WAV", b"RIFF voice memo", D(2026, 1, 23, 23, 2, 20))
    card.file("DCIM/100NCZ_F/DSC_0099.XMP", b"<x:xmpmeta/>", D(2026, 1, 23, 23, 5, 0))
    plan = make_plan([card.root], dest)
    assert item(plan, "DSC_0001.WAV").dest_rel.as_posix() == "RAW/DSC_0001.WAV"
    assert item(plan, "DSC_0099.XMP").action is Action.ORPHAN
    plan = make_plan([card.root], dest, opts=PlanOptions(copy_unknown=True))
    assert item(plan, "DSC_0099.XMP").dest_rel.as_posix() == "OTHER/DSC_0099.XMP"


# ---- blocking problems ------------------------------------------------------------------------


def test_not_enough_space_via_disk_free(pairs, dest, monkeypatch):
    monkeypatch.setattr(engine, "disk_free", lambda p: 100 * 1024 * 1024)
    plan = make_plan(pairs, dest)
    assert plan.blocked
    assert any("Not enough space" in p for p in plan.problems)
    assert plan.free_bytes == 100 * 1024 * 1024


def test_not_enough_space_via_disk_usage(pairs, dest, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda p: usage(10**9, 10**9, 1000))
    plan = make_plan(pairs, dest)
    assert any("Not enough space" in p for p in plan.problems)


def test_enough_space_is_not_a_problem(pairs, dest, monkeypatch):
    monkeypatch.setattr(engine, "disk_free", lambda p: 10 * 1024**3)
    assert not make_plan(pairs, dest).problems


def test_not_writable(pairs, dest, monkeypatch):
    monkeypatch.setattr(planner.os, "access", lambda p, mode: False)
    plan = make_plan(pairs, dest)
    assert any("Cannot write" in p for p in plan.problems)


def test_write_probe_failure_blocks(pairs, dest, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(planner, "open", denied, raising=False)
    plan = make_plan(pairs, dest, probe_write=True)
    assert any("Cannot write" in p and "Access is denied" in p for p in plan.problems)


def test_write_probe_leaves_nothing(pairs, dest):
    plan = make_plan(pairs, dest, probe_write=True)
    assert not plan.problems
    assert list(dest.iterdir()) == []


def test_destination_inside_a_card_is_blocked(tmp_path):
    roots = make_zf_cards(tmp_path / "own", PAIRS[:1], "raw_jpg", junk=False)
    inside = Path(roots[0]) / "Photos"
    inside.mkdir()
    plan = make_plan(roots, inside)
    assert any("inside each other" in p for p in plan.problems)


@pytest.mark.parametrize("where", ["root", "inside"])
def test_write_probe_never_touches_a_card(tmp_path, monkeypatch, where):
    roots = make_zf_cards(tmp_path / "own", PAIRS[:1], "raw_jpg", junk=False)
    target = Path(roots[0]) if where == "root" else Path(roots[0]) / "DCIM"
    opened: list[str] = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(planner, "open", spy, raising=False)
    before = FakeCard(roots[0]).state()
    plan = make_plan(roots, target, probe_write=True)
    assert any("inside each other" in p for p in plan.problems)
    assert opened == []
    assert FakeCard(roots[0]).state() == before


def test_card_inside_destination_is_blocked(tmp_path):
    lib_root = tmp_path / "lib"
    roots = make_zf_cards(lib_root / "cards", PAIRS[:1], "raw_jpg", junk=False)
    plan = make_plan(roots, lib_root)
    assert any("inside each other" in p for p in plan.problems)


def test_raw_exists_as_a_file(pairs, dest):
    write_file(lib(dest, "RAW"), b"not a folder")
    plan = make_plan(pairs, dest)
    assert any("RAW exists but is a file" in p for p in plan.problems)


def test_event_folder_exists_as_a_file(pairs, dest):
    write_file(dest / EVENT, b"not a folder")
    plan = make_plan(pairs, dest)
    assert plan.blocked
    assert any("not a folder" in w for w in plan.warnings + plan.problems)


def test_destination_missing(pairs, tmp_path):
    plan = make_plan(pairs, tmp_path / "nope")
    assert any("does not exist" in p for p in plan.problems)


def test_no_days_selected(pairs, dest):
    plan = make_plan(pairs, dest, days=[])
    assert "No days selected" in plan.problems
    assert all(i.action is Action.SKIP_DESELECTED for i in plan.items)


def test_windows_path_too_long(pairs, tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "sys", SimpleNamespace(platform="win32"))
    base = tmp_path / "deep"
    room = 200 - len(str(base))
    deep = base / ("d" * max(1, room))
    deep.mkdir(parents=True)
    plan = make_plan(pairs, deep)
    assert any("Path too long for Windows" in p for p in plan.problems)
    short = tmp_path / "s"
    short.mkdir()
    assert not make_plan(pairs, short).problems


@pytest.mark.skipif(os.name != "nt", reason="the Windows path limit only applies on Windows")
def test_windows_path_limit_counts_the_temp_name(pairs, tmp_path):
    # Exactly at the limit for the final name, over it for the ".part" temp name.
    plan = make_plan(pairs, tmp_path)
    longest = max(len(str(i.dest)) for i in plan.to_copy)
    pad = planner.WINDOWS_MAX_PATH - longest
    if pad <= 1:
        pytest.skip("temp folder path already too long")
    deep = tmp_path / ("p" * (pad - 1))
    deep.mkdir()
    plan = make_plan(pairs, deep)
    assert max(len(str(i.dest)) for i in plan.to_copy) <= planner.WINDOWS_MAX_PATH
    assert any("Path too long" in p for p in plan.problems)
