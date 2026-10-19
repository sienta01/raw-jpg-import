"""Copier under injected faults: read errors, corrupt writes, a full disk, a pulled card,
busy renames, a crash before the ledger write, cancellation.

Whatever happens: no partial file under a final name, no ``.part`` left behind, never an
overwritten file, exactly one outcome per scanned file, and a re-run finishes the job.
"""

from __future__ import annotations

import errno
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from fakes.card import (
    EVENT,
    Crash,
    FakeCard,
    FakeShot,
    FaultyFileOps,
    files_under,
    make_plan,
    make_zf_cards,
    no_real_cards,
    part_files,
    run_import,
    sha256_file,
)
from fakes.files import write_file

from raw_jpg_import import copier, engine
from raw_jpg_import.copier import ImportReport, Outcome
from raw_jpg_import.errors import AnotherImportRunning, PlanBlocked
from raw_jpg_import.events import CancelToken, FileDone, Progress
from raw_jpg_import.ledger import Ledger
from raw_jpg_import.planner import Action

D = datetime
SHOTS = [
    FakeShot("DSC_0001", D(2026, 1, 23, 23, 2, 10)),
    FakeShot("DSC_0002", D(2026, 1, 23, 23, 15, 42)),
    FakeShot("DSC_0003", D(2026, 1, 23, 23, 31, 5)),
]
ALL = {f"{folder}/DSC_000{n}.{ext}" for n in (1, 2, 3)
       for folder, ext in (("RAW", "NEF"), ("JPG", "JPG"))}


@pytest.fixture(autouse=True)
def _no_real_cards(monkeypatch):
    no_real_cards(monkeypatch)


@pytest.fixture(scope="module")
def cards(tmp_path_factory):
    """Read-only cards shared by the tests that do not change them (raw_jpg mode)."""
    roots = make_zf_cards(tmp_path_factory.mktemp("rj"), SHOTS, "raw_jpg", junk=False)
    state = [FakeCard(r).state() for r in roots]
    yield roots
    assert [FakeCard(r).state() for r in roots] == state, "a fault test changed the cards"


@pytest.fixture(scope="module")
def backup(tmp_path_factory):
    roots = make_zf_cards(tmp_path_factory.mktemp("bk"), SHOTS, "backup", junk=False)
    state = [FakeCard(r).state() for r in roots]
    yield roots
    assert [FakeCard(r).state() for r in roots] == state


@pytest.fixture
def own_cards(tmp_path):
    """Cards this test may change (pull, touch)."""
    return make_zf_cards(tmp_path / "own", SHOTS, "raw_jpg", junk=False)


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "Library"
    d.mkdir()
    return d


def outcomes(report: ImportReport) -> dict[str, str]:
    """``{"SLOT1:DSC_0001.NEF": "COPIED", ...}`` for every planned item."""
    assert set(report.results) == {i.id for i in report.plan.items}, "one outcome per file"
    out = {}
    for item in report.plan.items:
        out[f"{item.src.source.label}:{item.src.name}"] = report.results[item.id].outcome.name
    return out


def result(report: ImportReport, name: str, label: str | None = None):
    assert set(report.results) == {i.id for i in report.plan.items}, "one outcome per file"
    hits = [(i, report.results[i.id]) for i in report.plan.items
            if i.src.name == name and (label is None or i.src.source.label == label)]
    assert len(hits) == 1
    return hits[0][1]


def library(dest: Path) -> set[str]:
    """Files in the event folder, e.g. ``RAW/DSC_0001.NEF``."""
    return files_under(dest / EVENT)


def assert_clean(dest: Path) -> None:
    assert part_files(dest) == []


def assert_copies_match(dest: Path, roots) -> None:
    """Every library file is byte-identical to its card file and has its mtime."""
    for rel in library(dest):
        name = rel.split("/")[-1]
        src = next(p for r in roots for p in Path(r).rglob(name))
        dst = dest / EVENT / rel
        assert sha256_file(dst) == sha256_file(src), rel
        assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns, rel


# ---- read errors ------------------------------------------------------------------------------


def test_read_error_fails_that_file_only(cards, dest):
    ops = FaultyFileOps(read_error={"DSC_0002.NEF": 1000})
    report = run_import(cards, dest, ops=ops)
    assert report.status == "partial"
    res = result(report, "DSC_0002.NEF")
    assert res.outcome is Outcome.FAILED and "could not read from the card" in res.error
    assert library(dest) == ALL - {"RAW/DSC_0002.NEF"}
    assert report.counts()[Outcome.COPIED] == 5
    assert_clean(dest)
    assert_copies_match(dest, cards)
    # The re-run copies only what is missing.
    again = run_import(cards, dest)
    assert again.status == "ok" and again.counts()[Outcome.COPIED] == 1
    assert library(dest) == ALL


def test_read_error_promotes_the_backup_twin(backup, dest):
    ops = FaultyFileOps(read_error={"SLOT1/DCIM/100NCZ_F/DSC_0002.NEF": 0})
    report = run_import(backup, dest, ops=ops)
    main = result(report, "DSC_0002.NEF", "SLOT1")
    twin = result(report, "DSC_0002.NEF", "SLOT2")
    assert main.outcome is Outcome.FAILED
    assert twin.outcome is Outcome.COPIED and twin.error is None
    assert twin.dest == dest / EVENT / "RAW/DSC_0002.NEF"
    assert library(dest) == ALL
    assert_copies_match(dest, backup)
    # The other twins stay duplicates of their copied main file.
    other = result(report, "DSC_0001.NEF", "SLOT2")
    assert other.outcome is Outcome.DUPLICATE and other.dest == dest / EVENT / "RAW/DSC_0001.NEF"
    with Ledger.open() as led:
        assert led.get(twin.ledger_id).src_rel == "DCIM/100NCZ_F/DSC_0002.NEF"
    dplan = engine.plan_deletion(report)
    eligible = {(c.src.source.label, c.src.name) for c in dplan.candidates}
    assert ("SLOT2", "DSC_0002.NEF") in eligible and ("SLOT1", "DSC_0002.NEF") not in eligible


def test_both_twins_unreadable(backup, dest):
    ops = FaultyFileOps(read_error={"DSC_0002.NEF": 0})
    report = run_import(backup, dest, ops=ops)
    assert result(report, "DSC_0002.NEF", "SLOT1").outcome is Outcome.FAILED
    assert result(report, "DSC_0002.NEF", "SLOT2").outcome is Outcome.FAILED
    assert library(dest) == ALL - {"RAW/DSC_0002.NEF"}
    assert_clean(dest)


# ---- verification -----------------------------------------------------------------------------


def test_one_verify_mismatch_is_retried(cards, dest):
    ops = FaultyFileOps(flip={"DSC_0002.NEF": 1})
    report = run_import(cards, dest, ops=ops)
    assert report.status == "ok" and ops.flipped == 1
    assert result(report, "DSC_0002.NEF").outcome is Outcome.COPIED
    assert library(dest) == ALL
    assert_copies_match(dest, cards)
    assert_clean(dest)


def test_persistent_mismatch_fails_the_file(cards, dest):
    ops = FaultyFileOps(flip={"DSC_0002.NEF": 2})
    report = run_import(cards, dest, ops=ops)
    res = result(report, "DSC_0002.NEF")
    assert res.outcome is Outcome.FAILED and "did not match" in res.error
    assert report.status == "partial" and ops.flipped == 2
    assert library(dest) == ALL - {"RAW/DSC_0002.NEF"}
    assert_copies_match(dest, cards)
    assert_clean(dest)


def test_three_mismatches_abort_the_session(cards, dest):
    ops = FaultyFileOps(flip={"DSC_0001.NEF": 2, "DSC_0002.NEF": 1})
    report = run_import(cards, dest, ops=ops)
    assert report.status == "aborted" and "card reader" in report.abort_reason
    assert outcomes(report) == {
        "SLOT1:DSC_0001.NEF": "FAILED", "SLOT1:DSC_0002.NEF": "FAILED",
        "SLOT1:DSC_0003.NEF": "CANCELLED", "SLOT2:DSC_0001.JPG": "CANCELLED",
        "SLOT2:DSC_0002.JPG": "CANCELLED", "SLOT2:DSC_0003.JPG": "CANCELLED",
    }
    assert files_under(dest) == set()          # nothing copied, created folders removed
    assert report.created_dirs == []
    assert run_import(cards, dest).status == "ok"
    assert library(dest) == ALL


# ---- the destination --------------------------------------------------------------------------


def test_disk_full_aborts(cards, dest):
    ops = FaultyFileOps(enospc=["DSC_0002.NEF"])
    report = run_import(cards, dest, ops=ops)
    assert report.status == "aborted" and "full" in report.abort_reason
    assert outcomes(report) == {
        "SLOT1:DSC_0001.NEF": "COPIED", "SLOT1:DSC_0002.NEF": "FAILED",
        "SLOT1:DSC_0003.NEF": "CANCELLED", "SLOT2:DSC_0001.JPG": "CANCELLED",
        "SLOT2:DSC_0002.JPG": "CANCELLED", "SLOT2:DSC_0003.JPG": "CANCELLED",
    }
    assert library(dest) == {"RAW/DSC_0001.NEF"}
    assert_clean(dest)


def test_file_too_large_for_the_drive(cards, dest):
    ops = FaultyFileOps(write_error={"DSC_0002.NEF": OSError(errno.EFBIG, "File too large")})
    report = run_import(cards, dest, ops=ops)
    res = result(report, "DSC_0002.NEF")
    assert res.outcome is Outcome.FAILED and "4 GB" in res.error
    assert report.status == "partial"
    assert_clean(dest)


def test_other_write_error_fails_the_file(cards, dest):
    ops = FaultyFileOps(write_error={"DSC_0002.NEF": OSError(errno.EIO, "I/O error")})
    report = run_import(cards, dest, ops=ops)
    res = result(report, "DSC_0002.NEF")
    assert res.outcome is Outcome.FAILED and "could not write" in res.error
    assert library(dest) == ALL - {"RAW/DSC_0002.NEF"}
    assert_clean(dest)


def test_folder_creation_error_fails_those_files_only(cards, dest):
    ops = FaultyFileOps(makedirs_error={"JPG": PermissionError(errno.EACCES, "Access denied")})
    report = run_import(cards, dest, ops=ops)
    assert report.status == "partial"
    for n in (1, 2, 3):
        res = result(report, f"DSC_000{n}.JPG")
        assert res.outcome is Outcome.FAILED and "Access denied" in res.error
    assert library(dest) == {"RAW/DSC_0001.NEF", "RAW/DSC_0002.NEF", "RAW/DSC_0003.NEF"}


def test_folder_creation_disk_full_aborts(cards, dest):
    ops = FaultyFileOps(makedirs_error={"JPG": OSError(errno.ENOSPC, "No space left")})
    report = run_import(cards, dest, ops=ops)
    assert report.status == "aborted" and "full" in report.abort_reason
    assert result(report, "DSC_0001.JPG").outcome is Outcome.FAILED
    assert result(report, "DSC_0002.JPG").outcome is Outcome.CANCELLED


def test_folder_turned_into_a_file_after_planning(cards, dest):
    plan = make_plan(cards, dest)
    write_file(dest / EVENT / "JPG", b"in the way")
    report = engine.execute(plan)
    assert report.status == "partial"
    assert all(result(report, f"DSC_000{n}.JPG").outcome is Outcome.FAILED for n in (1, 2, 3))
    assert all(result(report, f"DSC_000{n}.NEF").outcome is Outcome.COPIED for n in (1, 2, 3))
    assert (dest / EVENT / "JPG").read_bytes() == b"in the way"


def test_name_taken_after_planning_is_never_replaced(cards, dest):
    plan = make_plan(cards, dest)
    write_file(dest / EVENT / "RAW/DSC_0002.NEF", b"someone else's file")
    nef3 = next(Path(cards[0]).rglob("DSC_0003.NEF"))
    (dest / EVENT / "RAW").mkdir(exist_ok=True)
    shutil.copy2(nef3, dest / EVENT / "RAW/DSC_0003.NEF")
    report = engine.execute(plan)
    taken = result(report, "DSC_0002.NEF")
    assert taken.outcome is Outcome.FAILED and "appeared since planning" in taken.error
    assert (dest / EVENT / "RAW/DSC_0002.NEF").read_bytes() == b"someone else's file"
    same = result(report, "DSC_0003.NEF")
    assert same.outcome is Outcome.ALREADY_THERE and same.ledger_id is not None
    assert_clean(dest)


def test_blocked_plan_is_refused(cards, dest, monkeypatch):
    monkeypatch.setattr(engine, "disk_free", lambda p: 1)
    plan = make_plan(cards, dest)
    with pytest.raises(PlanBlocked):
        engine.execute(plan)
    assert files_under(dest) == set()


def test_stale_part_files_are_cleaned(cards, dest):
    stale = write_file(dest / EVENT / "RAW/.DSC_0001.NEF.rji-abc123.part", b"old", D(2020, 1, 1))
    fresh = write_file(dest / EVENT / "RAW/.DSC_0002.NEF.rji-def456.part", b"busy")
    report = run_import(cards, dest)
    assert report.status == "ok"
    assert not stale.exists() and fresh.exists()


# ---- renames ----------------------------------------------------------------------------------


def test_busy_rename_is_retried(cards, dest):
    ops = FaultyFileOps(rename_busy=2)
    report = run_import(cards, dest, ops=ops)
    assert report.status == "ok" and library(dest) == ALL
    assert ops.sleeps == [0.1, 0.2]
    assert_clean(dest)


def test_rename_busy_for_too_long_fails_the_file(cards, dest):
    ops = FaultyFileOps(rename_busy=7)          # 1 attempt + 6 retries
    report = run_import(cards, dest, ops=ops)
    res = result(report, "DSC_0001.NEF")
    assert res.outcome is Outcome.FAILED and "could not write" in res.error
    assert library(dest) == ALL - {"RAW/DSC_0001.NEF"}
    assert_clean(dest)


def test_no_hard_links_falls_back_to_guarded_rename(cards, dest):
    ops = FaultyFileOps(link_eperm=True)
    report = run_import(cards, dest, ops=ops)
    assert report.status == "ok" and library(dest) == ALL
    assert ops.renames == 6
    assert_copies_match(dest, cards)
    assert_clean(dest)


# ---- crash and cancel -------------------------------------------------------------------------


def test_crash_between_rename_and_ledger_write(cards, dest):
    ops = FaultyFileOps(crash_after_rename=["DSC_0002.NEF"])
    with pytest.raises(Crash):
        run_import(cards, dest, ops=ops)
    final = dest / EVENT / "RAW/DSC_0002.NEF"
    assert final.is_file()
    assert_clean(dest)
    with Ledger.open() as led:
        assert led.find("dsc_0002.nef", final.stat().st_size, None) == []
        assert len(led.find("dsc_0001.nef", (dest / EVENT / "RAW/DSC_0001.NEF").stat().st_size,
                            None)) == 1

    plan = make_plan(cards, dest)
    item = next(i for i in plan.items if i.src.name == "DSC_0002.NEF")
    assert item.action is Action.SKIP_EXISTS
    report = engine.execute(plan)
    res = report.results[item.id]
    assert res.outcome is Outcome.ALREADY_THERE
    with Ledger.open() as led:
        row = led.get(res.ledger_id)
    assert row.kind == "adopted" and row.dest_rel == "RAW/DSC_0002.NEF"
    assert library(dest) == ALL
    assert report.counts()[Outcome.COPIED] == 4
    assert_copies_match(dest, cards)


def test_cancel_mid_file(cards, dest, monkeypatch):
    small = 16 * 1024
    monkeypatch.setattr(copier, "CHUNK", small)         # several chunks per fake NEF
    token = CancelToken()
    parts_seen: list[tuple[str, int]] = []

    def hook(path: Path, n: int) -> None:
        if path.name == "DSC_0002.NEF" and n > small and not token.is_set():
            parts_seen.extend((p.name, p.stat().st_size) for p in part_files(dest))
            token.cancel()

    report = run_import(cards, dest, ops=FaultyFileOps(on_chunk=hook), cancel=token)
    assert report.status == "cancelled"
    assert [name for name, _ in parts_seen] == [f".DSC_0002.NEF.rji-{report.plan.session_id[-6:]}"
                                               ".part"]
    assert outcomes(report) == {
        "SLOT1:DSC_0001.NEF": "COPIED", "SLOT1:DSC_0002.NEF": "CANCELLED",
        "SLOT1:DSC_0003.NEF": "CANCELLED", "SLOT2:DSC_0001.JPG": "CANCELLED",
        "SLOT2:DSC_0002.JPG": "CANCELLED", "SLOT2:DSC_0003.JPG": "CANCELLED",
    }
    assert_clean(dest)
    assert library(dest) == {"RAW/DSC_0001.NEF"}
    assert not (dest / EVENT / "JPG").exists()
    assert [c.src.name for c in engine.plan_deletion(report).candidates] == ["DSC_0001.NEF"]

    again = run_import(cards, dest)
    assert again.status == "ok"
    assert again.counts() == {Outcome.ALREADY_THERE: 1, Outcome.COPIED: 5}
    assert library(dest) == ALL
    assert_copies_match(dest, cards)


def test_cancel_before_start(cards, dest):
    token = CancelToken()
    token.cancel()
    report = run_import(cards, dest, cancel=token)
    assert report.status == "cancelled"
    assert set(outcomes(report).values()) == {"CANCELLED"}
    assert files_under(dest) == set()


# ---- the cards --------------------------------------------------------------------------------


def test_card_pulled_mid_import(own_cards, dest):
    slot1 = Path(own_cards[0])
    ops = FaultyFileOps(vanish=("DSC_0002.NEF", slot1, 1000))
    report = run_import(own_cards, dest, ops=ops)
    assert report.status == "partial"
    assert outcomes(report) == {
        "SLOT1:DSC_0001.NEF": "COPIED", "SLOT1:DSC_0002.NEF": "FAILED",
        "SLOT1:DSC_0003.NEF": "FAILED", "SLOT2:DSC_0001.JPG": "COPIED",
        "SLOT2:DSC_0002.JPG": "COPIED", "SLOT2:DSC_0003.JPG": "COPIED",
    }
    assert result(report, "DSC_0002.NEF").error == "card removed"
    assert result(report, "DSC_0003.NEF").error == "card removed"
    assert not any(p.name == "DSC_0003.NEF" for p in ops.opened)    # not even tried
    assert not slot1.exists() and ops.vanished
    assert_clean(dest)


@pytest.fixture
def touched(cards):
    """Lets a test change a shared card file's time; restores it afterwards."""
    nef = next(Path(cards[0]).rglob("DSC_0002.NEF"))
    st = nef.stat()
    yield nef
    os.utime(nef, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_source_changed_since_scan(cards, dest, touched):
    plan = make_plan(cards, dest)
    os.utime(touched, ns=(0, touched.stat().st_mtime_ns + 5_000_000_000))
    report = engine.execute(plan)
    res = result(report, "DSC_0002.NEF")
    assert res.outcome is Outcome.FAILED and "scan again" in res.error
    assert library(dest) == ALL - {"RAW/DSC_0002.NEF"}


def test_source_changed_while_copying(cards, dest, touched):
    def touch(path: Path, n: int) -> None:
        if path == touched:
            st = path.stat()
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    report = run_import(cards, dest, ops=FaultyFileOps(on_chunk=touch))
    res = result(report, "DSC_0002.NEF")
    assert res.outcome is Outcome.FAILED and "while copying" in res.error
    assert library(dest) == ALL - {"RAW/DSC_0002.NEF"}
    assert_clean(dest)


# ---- the session ------------------------------------------------------------------------------


def test_import_lock_held(cards, dest):
    plan = make_plan(cards, dest)
    with engine.import_lock(), pytest.raises(AnotherImportRunning):
        engine.execute(plan)
    assert files_under(dest) == set()


def test_progress_and_file_events(cards, dest):
    events: list[object] = []
    plan = make_plan(cards, dest)
    report = engine.execute(plan, emit=events.append)
    done = [e for e in events if isinstance(e, FileDone)]
    assert sorted(e.item_id for e in done) == sorted(i.id for i in plan.to_copy)
    assert all(e.outcome == "COPIED" for e in done)
    last = [e for e in events if isinstance(e, Progress)][-1]
    assert last.phase == "copy" and last.files_done == last.files_total == 6
    assert last.bytes_done == last.bytes_total == plan.bytes_to_copy == report.bytes_copied


def test_rescue_aborted_by_full_disk_reports_aborted(backup, dest):
    ops = FaultyFileOps(read_error={"SLOT1/DCIM/100NCZ_F/DSC_0002.NEF": 0},
                        enospc=["DSC_0002.NEF"])
    report = run_import(backup, dest, ops=ops)
    assert report.status == "aborted" and "full" in report.abort_reason
    assert result(report, "DSC_0002.NEF", "SLOT1").outcome is Outcome.FAILED
    twin = result(report, "DSC_0002.NEF", "SLOT2")
    assert twin.outcome is Outcome.FAILED and "full" in twin.error
    assert_clean(dest)
    dplan = engine.plan_deletion(report)
    assert all(c.src.name != "DSC_0002.NEF" for c in dplan.candidates)


def test_copier_refuses_without_lock_holder_problems(cards, dest):
    plan = make_plan(cards, dest)
    plan.problems.append("blocked")
    with pytest.raises(PlanBlocked):
        copier.execute(plan, ledger=Ledger.memory())
