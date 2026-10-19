"""End to end: fake Zf cards -> scan -> plan -> copy -> library, through the engine facade."""

from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path

import pytest
from fakes.card import (
    DAY1,
    DAY2,
    EVENT,
    EVENT_NAME,
    FakeCard,
    FakeShot,
    FaultyFileOps,
    ZfCards,
    cancel_after,
    expected_files,
    files_under,
    forget_history,
    make_plan,
    make_zf_cards,
    part_files,
    run_import,
    sha256_file,
    sunset_shots,
    zf_cards,
)

from raw_jpg_import import engine
from raw_jpg_import.copier import CHUNK, ImportReport, Outcome
from raw_jpg_import.events import CancelToken
from raw_jpg_import.fsops import is_readonly
from raw_jpg_import.ledger import Ledger
from raw_jpg_import.planner import Action, PlanOptions

D = datetime


# A shorter walk for most tests: rollover DSC_0001, a clip, a shot after midnight.
SMALL = [
    FakeShot("DSC_0001", D(2026, 1, 23, 18, 0, 0)),
    FakeShot("DSC_0002", D(2026, 1, 23, 23, 2, 10)),
    FakeShot("DSC_0003", D(2026, 1, 23, 23, 20, 0), quality="MOV"),
    FakeShot("DSC_0001", D(2026, 1, 23, 23, 40, 0), folder="101NCZ_F"),
    FakeShot("DSC_0004", D(2026, 1, 24, 0, 15, 0), folder="101NCZ_F"),
]


def _readonly_cards(tmp_path_factory, **kw):
    z = zf_cards(tmp_path_factory.mktemp("cards"), **kw)
    before = z.state()
    yield z
    assert z.state() == before, "an import changed the cards"


@pytest.fixture(scope="module")
def zf_ro(tmp_path_factory) -> ZfCards:
    """The whole sunset walk, built once; only read by the tests that use it."""
    yield from _readonly_cards(tmp_path_factory)


@pytest.fixture(scope="module")
def small(tmp_path_factory) -> ZfCards:
    """:data:`SMALL` on two cards plus ``NOTES.XYZ``, built once and only read."""
    yield from _readonly_cards(tmp_path_factory, shots=SMALL)


def tree(z: ZfCards | None = None, files: set[str] | None = None) -> set[str]:
    """Expected library paths below the destination root, for cards ``z`` or ``files``."""
    rels = files if files is not None else z.expected()
    return {f"{EVENT}/{rel}" for rel in rels}


def check_copies(report: ImportReport) -> None:
    """Copied files: same bytes and mtime as the card, writable, no temp files."""
    for item in report.plan.items:
        res = report.results[item.id]
        if res.outcome is not Outcome.COPIED:
            continue
        dst, src = res.dest, item.src.path
        assert dst is not None and dst.is_file()
        assert res.sha256 == sha256_file(dst) == sha256_file(src)
        assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns
        assert not is_readonly(dst.stat()), dst
    assert part_files(report.plan.dest_root) == []


# ---- the full import --------------------------------------------------------------------------


def test_full_two_card_import_then_rerun(zf_ro, dest):
    report = run_import(zf_ro.roots, dest)
    assert report.status == "ok", report.failed()
    assert files_under(dest) == tree(zf_ro)
    assert sorted(p.name for p in (dest / EVENT).iterdir()) == ["JPG", "MOV", "RAW"]
    check_copies(report)
    # Every scanned file has exactly one outcome; junk was never even listed.
    names = {i.src.name for i in report.plan.items}
    assert len(report.results) == len(report.plan.items) == 22
    assert not names & {"NC_FLLST.DAT", "NIKON001.DSC", "._DSC_0001.NEF", "IndexerVolumeGuid"}
    assert report.counts() == {Outcome.COPIED: 21, Outcome.NOT_IMPORTED: 1}
    assert report.bytes_copied == report.plan.bytes_to_copy
    assert report.folders == [dest / EVENT]
    assert set(report.created_dirs) == {dest / EVENT, *(dest / EVENT / f for f in
                                                         ("RAW", "JPG", "MOV"))}
    # The protected NEF was copied, but its copy is an ordinary writable file.
    protected = next(i for i in report.plan.items if i.src.readonly)
    assert protected.src.name == "DSC_0004.NEF"
    assert os.access(report.results[protected.id].dest, os.W_OK)
    # Rollover: both files of the 23:40 DSC_0001 get -2.
    rollover = {i.dest_rel.as_posix() for i in report.plan.items
                if i.src.rel.parent.name == "101NCZ_F" and i.src.stem == "DSC_0001"}
    assert rollover == {"RAW/DSC_0001-2.NEF", "JPG/DSC_0001-2.JPG"}
    # History: one "copied" row per file.
    with Ledger.open() as led:
        rows = led.rows_for_event(dest, EVENT)
    assert len(rows) == 21 and {r.kind for r in rows} == {"copied"}
    assert all(r.sha256 for r in rows)
    # Session log lists every file.
    log = report.log_path.read_text(encoding="utf-8")
    assert "status ok" in log and "NOTES.XYZ" in log and "DSC_0001-2.NEF" in log

    # Run again: nothing is copied, with the history and without it.
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in (dest / EVENT).rglob("*")}
    again = run_import(zf_ro.roots, dest)
    assert again.plan.to_copy == [] and again.bytes_copied == 0
    assert again.counts() == {Outcome.ALREADY_THERE: 21, Outcome.NOT_IMPORTED: 1}
    assert again.plan.counts()[Action.SKIP_IMPORTED] == 21

    forget_history()
    third = run_import(zf_ro.roots, dest)
    assert third.plan.to_copy == [] and third.bytes_copied == 0
    assert third.plan.counts()[Action.SKIP_EXISTS] == 21
    with Ledger.open() as led:
        rows = led.rows_for_event(dest, EVENT)
    assert len(rows) == 21 and {r.kind for r in rows} == {"adopted"}
    assert make_plan(zf_ro.roots, dest).counts()[Action.SKIP_IMPORTED] == 21
    assert {p: (p.stat().st_mtime_ns, p.stat().st_size)
            for p in (dest / EVENT).rglob("*")} == before


def test_unknown_copy_into_other(small, dest):
    report = run_import(small.roots, dest, opts=PlanOptions(copy_unknown=True))
    assert report.status == "ok"
    other = dest / EVENT / "OTHER" / "NOTES.XYZ"
    assert sha256_file(other) == sha256_file(small.slot1.find("NOTES.XYZ"))
    assert files_under(dest) == tree(small) | {f"{EVENT}/OTHER/NOTES.XYZ"}


def test_days_imported_separately(small, dest):
    first = run_import(small.roots, dest, day_start=time(0, 0), days=[DAY1])
    late = [i for i in first.plan.items if i.capture and i.capture.local.date() == DAY2]
    assert late and all(first.results[i.id].outcome is Outcome.NOT_IMPORTED for i in late)
    second = run_import(small.roots, dest, day_start=time(0, 0), days=[DAY2],
                        name="After Midnight")
    assert second.counts()[Outcome.COPIED] == 2
    assert sorted(p.name for p in dest.iterdir()) == [EVENT, "2026.01.24 - After Midnight"]
    assert files_under(dest / "2026.01.24 - After Midnight") == {"RAW/DSC_0004.NEF",
                                                                 "JPG/DSC_0004.JPG"}


# ---- one card now, the other one later -------------------------------------------------------


@pytest.mark.parametrize("first_slot, history", [(1, "kept"), (2, "kept"), (1, "deleted"),
                                                  (2, "deleted")])
def test_second_card_imported_later(small, dest, history, first_slot):
    first, second = small.roots if first_slot == 1 else small.roots[::-1]
    one = run_import([first], dest)
    assert one.status == "ok"
    if history == "deleted":
        forget_history()
    two = run_import([second], dest)
    assert two.status == "ok"
    assert two.plan.targets[0].target.exists
    assert files_under(dest) == tree(small)
    rollover = [i for i in two.plan.items
                if i.src.rel.parent.name == "101NCZ_F" and i.src.stem == "DSC_0001"]
    assert len(rollover) == 1 and rollover[0].dest_rel.name.startswith("DSC_0001-2.")
    check_copies(two)
    assert two.plan.warnings == [] or all("unknown type" in w for w in two.plan.warnings)


def test_new_shots_added_to_the_cards_later(tmp_path, dest):
    shots = sunset_shots()[:4]
    z = zf_cards(tmp_path / "cards", shots=shots, junk=False, unknown=False)
    run_import(z.roots, dest)
    late = FakeShot("DSC_0005", D(2026, 1, 23, 23, 50, 0))
    z.slot1.photo("DSC_0005.NEF", late.when, subsec=late.subsec_text)
    z.slot2.photo("DSC_0005.JPG", late.when, subsec=late.subsec_text)
    again = run_import(z.roots, dest)
    assert {i.dest_rel.as_posix() for i in again.plan.to_copy} == {"RAW/DSC_0005.NEF",
                                                                   "JPG/DSC_0005.JPG"}
    assert again.counts()[Outcome.ALREADY_THERE] == 8


# ---- other card modes -------------------------------------------------------------------------


def _mode_shots(mode: str) -> list[FakeShot]:
    q = {"raw_heif": "RAW+HIF"}.get(mode, "RAW+JPG")
    return [
        FakeShot("DSC_0001", D(2026, 1, 23, 18, 0, 0), quality=q),
        FakeShot("DSC_0002", D(2026, 1, 23, 23, 2, 10), quality=q),
        FakeShot("DSC_0003", D(2026, 1, 23, 23, 20, 0), quality="MOV"),
        FakeShot("DSC_0001", D(2026, 1, 23, 23, 40, 0), quality=q, folder="101NCZ_F"),
    ]


@pytest.mark.parametrize("mode", ["backup", "overflow", "jpg_jpg", "raw_heif"])
def test_card_modes(tmp_path, dest, mode):
    shots = _mode_shots(mode)
    roots = make_zf_cards(tmp_path / mode, shots, mode, junk=True)
    report = run_import(roots, dest)
    assert report.status == "ok"
    assert files_under(dest) == tree(files=expected_files(shots, mode))
    check_copies(report)
    if mode == "backup":
        dups = [i for i in report.plan.items if i.action is Action.SKIP_DUPLICATE]
        assert len(dups) == 7
        for d in dups:
            res = report.results[d.id]
            assert res.outcome is Outcome.DUPLICATE
            assert res.dest == report.results[d.dup_of].dest and res.sha256
    again = run_import(roots, dest)
    assert again.bytes_copied == 0 and again.plan.to_copy == []


def test_raw_jpg_without_video_creates_only_raw_and_jpg(tmp_path, dest):
    shots = [s for s in _mode_shots("raw_jpg") if s.quality != "MOV"]
    roots = make_zf_cards(tmp_path / "rj", shots, "raw_jpg")
    run_import(roots, dest)
    assert sorted(p.name for p in (dest / EVENT).iterdir()) == ["JPG", "RAW"]
    assert sorted(p.name for p in dest.iterdir()) == [EVENT]


# ---- folders the import created ---------------------------------------------------------------


def test_empty_created_folders_are_removed(small, dest):
    report = run_import(small.roots, dest, ops=FaultyFileOps(read_error={"*": 0}))
    assert report.status == "partial"
    assert report.counts()[Outcome.FAILED] == 9
    assert list(dest.iterdir()) == []
    assert report.created_dirs == []


def test_only_the_failed_folder_is_removed(small, dest):
    report = run_import(small.roots, dest, ops=FaultyFileOps(read_error={"DSC_0003.MOV": 10}))
    assert report.status == "partial"
    assert sorted(p.name for p in (dest / EVENT).iterdir()) == ["JPG", "RAW"]
    assert dest / EVENT / "MOV" not in report.created_dirs


def test_existing_folders_are_never_removed(small, dest):
    (dest / EVENT / "MOV").mkdir(parents=True)
    report = run_import(small.roots, dest, ops=FaultyFileOps(read_error={"*": 0}))
    assert report.counts()[Outcome.FAILED] == 9
    assert (dest / EVENT / "MOV").is_dir()


# ---- the same event, another name --------------------------------------------------------------


def test_existing_event_folder_is_reused_under_its_real_name(small, dest):
    (dest / "2026.01.23 - sunset beachwalk").mkdir()
    report = run_import(small.roots, dest, name=EVENT_NAME)
    assert sorted(p.name for p in dest.iterdir()) == ["2026.01.23 - sunset beachwalk"]
    assert report.plan.targets[0].target.exists


def test_same_card_given_twice_is_copied_once(small, dest):
    root = small.roots[0]
    report = run_import([root, root], dest)
    assert report.status == "ok"
    assert report.plan.counts()[Action.SKIP_DUPLICATE] == report.plan.counts()[Action.COPY] == 5
    assert files_under(dest) == {f"{EVENT}/{p}" for p in (
        "RAW/DSC_0001.NEF", "RAW/DSC_0002.NEF", "MOV/DSC_0003.MOV", "RAW/DSC_0001-2.NEF",
        "RAW/DSC_0004.NEF")}
    check_copies(report)


def test_cancel_inside_a_big_clip_then_resume(tmp_path, dest):
    """A clip larger than the copy chunk, cancelled after its first chunk was written."""
    shots = [FakeShot("DSC_0001", D(2026, 1, 23, 23, 2, 10)),
             FakeShot("DSC_0002", D(2026, 1, 23, 23, 5, 0), quality="MOV")]
    roots = make_zf_cards(tmp_path / "big", shots, "raw_jpg", junk=False,
                          big_video=9 * 1024 * 1024)          # streamed to disk by the fake
    clip = next(Path(roots[0]).rglob("DSC_0002.MOV"))
    assert clip.stat().st_size == 9 * 1024 * 1024
    token = CancelToken()
    ops = FaultyFileOps(on_chunk=cancel_after(token, "DSC_0002.MOV", CHUNK + 1))
    report = run_import(roots, dest, ops=ops, cancel=token)
    assert report.status == "cancelled"
    assert {i.src.name: report.results[i.id].outcome for i in report.plan.items} == {
        "DSC_0001.NEF": Outcome.COPIED, "DSC_0002.MOV": Outcome.CANCELLED,
        "DSC_0001.JPG": Outcome.CANCELLED}
    assert part_files(dest) == [] and not (dest / EVENT / "MOV").exists()
    again = run_import(roots, dest)
    assert again.status == "ok" and again.counts()[Outcome.COPIED] == 2
    check_copies(again)


def test_import_works_without_a_usable_history(small, dest, monkeypatch):
    import sqlite3

    def broken(cls, path=None):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(Ledger, "open", classmethod(broken))
    report = run_import(small.roots, dest)
    assert report.status == "ok" and report.counts()[Outcome.COPIED] == 9
    assert engine.HISTORY_UNAVAILABLE in report.plan.warnings
    check_copies(report)
    again = run_import(small.roots, dest)          # disk checks alone prevent duplicates
    assert again.plan.to_copy == [] and again.plan.counts()[Action.SKIP_EXISTS] == 9


# ---- scripts/make_fake_cards.py (the manual end-to-end recipe) --------------------------------


def _script():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "make_fake_cards.py"
    spec = importlib.util.spec_from_file_location("make_fake_cards", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_make_fake_cards_script(tmp_path, capsys):
    script = _script()
    base = tmp_path / "rjitest"
    assert script.main([str(base)]) == 0
    out = capsys.readouterr().out
    assert "RAW   10:" in out and "DSC_0001-2.NEF" in out and "BACKUP_A" in out
    assert sorted(p.name for p in base.iterdir()) == ["BACKUP_A", "BACKUP_B", "MICROSD",
                                                      "Photos", "SD", "snapshot.json"]
    assert list((base / "Photos").iterdir()) == []
    assert script.main([str(base)]) == 2                  # never builds over an earlier run
    assert script.main(["--verify", str(base)]) == 0
    assert "All cards unchanged." in capsys.readouterr().out

    (base / "SD" / "DCIM/100NCZ_F/DSC_0002.NEF").unlink()
    nef = base / "SD" / "DCIM/100NCZ_F/DSC_0003.NEF"
    os.utime(nef, ns=(0, nef.stat().st_mtime_ns + 2_000_000_000))
    assert script.main(["--verify", str(base)]) == 1
    out = capsys.readouterr().out
    assert "SD        CHANGED (2 differences)" in out
    assert "missing: DCIM/100NCZ_F/DSC_0002.NEF" in out
    assert "changed time: DCIM/100NCZ_F/DSC_0003.NEF" in out
    assert "BACKUP_A  unchanged" in out


def test_make_fake_cards_refuses_a_card(tmp_path, capsys):
    script = _script()
    card = FakeCard(tmp_path / "looks like a card")
    assert script.main([str(card.root)]) == 2
    assert "DCIM" in capsys.readouterr().err
    assert not (card.root / "SD").exists()


def test_damaged_history_is_reported_in_the_plan(small, dest):
    from raw_jpg_import import appdirs

    run_import(small.roots, dest)
    db = appdirs.ledger_file()
    for p in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        p.unlink(missing_ok=True)
    db.write_bytes(b"this is not a database" * 100)
    engine.existing_events(dest)                     # the first open sets the bad file aside
    plan = make_plan(small.roots, dest)
    assert any("import history was damaged" in w for w in plan.warnings)
    assert plan.to_copy == []                        # the disk checks still find everything
    again = make_plan(small.roots, dest)             # still true for the rest of the session
    assert sum("import history was damaged" in w for w in again.warnings) == 1
