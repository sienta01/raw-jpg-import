"""Opt-in deletion of verified originals, on fake cards only.

A card file is deleted only when the import put it (or its identical twin) in the library
and fresh hashes of the card file and of the library copy agree. Everything else stays.
"""

from __future__ import annotations

import dataclasses
import os
import stat
from datetime import datetime, time
from pathlib import Path

import pytest
from fakes.card import (
    DAY1,
    DAY2,
    EVENT,
    JUNK_FILES,
    FakeCard,
    FakeShot,
    FaultyFileOps,
    files_under,
    forget_history,
    make_zf_cards,
    run_import,
    sha256_file,
)

from raw_jpg_import import deleter, engine
from raw_jpg_import.copier import ImportReport
from raw_jpg_import.deleter import DeleteOutcome, DeletionPlan
from raw_jpg_import.errors import AnotherImportRunning
from raw_jpg_import.events import CancelToken, FileDone
from raw_jpg_import.fsops import RealFileOps
from raw_jpg_import.ledger import Ledger

D = datetime
SHOTS = [
    FakeShot("DSC_0001", D(2026, 1, 23, 23, 2, 10)),
    FakeShot("DSC_0002", D(2026, 1, 23, 23, 15, 42), protect=True),
    FakeShot("DSC_0003", D(2026, 1, 23, 23, 31, 5)),
]


@pytest.fixture
def cards(tmp_path) -> tuple[FakeCard, FakeCard]:
    r1, r2 = make_zf_cards(tmp_path / "cards", SHOTS, "raw_jpg", junk=True)
    c1, c2 = FakeCard(r1), FakeCard(r2)
    c1.unknown("NOTES.XYZ", when=D(2026, 1, 23, 23, 3, 0))
    return c1, c2


def roots(cards) -> list[Path]:
    return [c.root for c in cards]


def imported(cards, dest, **kw) -> ImportReport:
    report = run_import(roots(cards), dest, **kw)
    assert report.status in ("ok", "partial")
    return report


def candidates(dplan: DeletionPlan) -> set[str]:
    return {f"{c.src.source.label}:{c.src.name}" for c in dplan.candidates}


def excluded(dplan: DeletionPlan) -> dict[str, str]:
    return {f"{f.source.label}:{f.name}": reason for f, reason in dplan.excluded}


def outcome_of(dreport, name: str, label: str | None = None) -> tuple[DeleteOutcome, str]:
    hits = [r for r in dreport.results
            if r.src.name == name and (label is None or r.src.source.label == label)]
    assert len(hits) == 1
    return hits[0].outcome, hits[0].reason


def delete(dplan: DeletionPlan, **kw):
    return engine.delete_originals(dplan, confirmed=True, **kw)


# ---- the happy path ---------------------------------------------------------------------------


def test_only_verified_copies_are_deleted(cards, dest):
    dirs_before = [c.dirs() for c in cards]
    report = imported(cards, dest)
    library = {p: sha256_file(dest / p) for p in files_under(dest)}
    dplan = engine.plan_deletion(report)
    assert candidates(dplan) == {"SLOT1:DSC_0001.NEF", "SLOT1:DSC_0003.NEF",
                                 "SLOT2:DSC_0001.JPG", "SLOT2:DSC_0002.JPG",
                                 "SLOT2:DSC_0003.JPG"}
    assert excluded(dplan) == {
        "SLOT1:DSC_0002.NEF": "not deleted: protected in the camera",
        "SLOT1:NOTES.XYZ": "not deleted: unknown file type, not imported",
    }
    assert dplan.per_source == {
        "SLOT1": (2, sum(c.src.size for c in dplan.candidates if c.src.source.label == "SLOT1")),
        "SLOT2": (3, sum(c.src.size for c in dplan.candidates if c.src.source.label == "SLOT2")),
    }

    dreport = delete(dplan, log_path=report.log_path)
    assert dreport.status == "ok"
    assert dreport.counts() == {DeleteOutcome.DELETED: 5}
    assert dreport.bytes_freed == dplan.bytes
    # What is left on the cards: the protected NEF, the unknown file, system files, folders.
    assert set(cards[0].snapshot()) == JUNK_FILES | {"DCIM/100NCZ_F/DSC_0002.NEF",
                                               "DCIM/100NCZ_F/NOTES.XYZ"}
    assert set(cards[1].snapshot()) == JUNK_FILES
    assert [c.dirs() for c in cards] == dirs_before
    assert cards[0].protected() == {"DCIM/100NCZ_F/DSC_0002.NEF"}
    # The library is untouched.
    assert {p: sha256_file(dest / p) for p in files_under(dest)} == library
    # History and log.
    with Ledger.open() as led:
        for c in dplan.candidates:
            row = led.get(c.ledger_id)
            assert row is not None and row.deleted_at is not None
    log = report.log_path.read_text(encoding="utf-8")
    assert "Deleted from cards: 5 files" in log


def test_junk_and_camera_files_are_untouched(cards, dest):
    before = {rel: v for rel, v in cards[0].snapshot().items() if rel in JUNK_FILES}
    delete(engine.plan_deletion(imported(cards, dest)))
    after = cards[0].snapshot()
    assert {rel: after[rel] for rel in JUNK_FILES} == before


def test_delete_after_a_rerun_uses_the_recorded_hash(cards, dest):
    imported(cards, dest)
    again = imported(cards, dest)
    dplan = engine.plan_deletion(again)
    assert len(dplan.candidates) == 5
    assert all(c.recorded_sha for c in dplan.candidates)
    assert delete(dplan).counts() == {DeleteOutcome.DELETED: 5}


def test_delete_after_a_rerun_without_history(cards, dest):
    imported(cards, dest)
    forget_history()
    again = imported(cards, dest)
    dplan = engine.plan_deletion(again)
    assert len(dplan.candidates) == 5 and not any(c.recorded_sha for c in dplan.candidates)
    dreport = delete(dplan)
    assert dreport.counts() == {DeleteOutcome.DELETED: 5}
    with Ledger.open() as led:                       # the adopted rows are marked
        assert all(led.get(c.ledger_id).deleted_at for c in dplan.candidates)


def test_backup_twins_are_both_deleted(tmp_path, dest):
    r1, r2 = make_zf_cards(tmp_path / "bk", SHOTS[:1], "backup", junk=False)
    report = run_import([r1, r2], dest)
    dplan = engine.plan_deletion(report)
    assert candidates(dplan) == {"SLOT1:DSC_0001.NEF", "SLOT1:DSC_0001.JPG",
                                 "SLOT2:DSC_0001.NEF", "SLOT2:DSC_0001.JPG"}
    twin = next(c for c in dplan.candidates if c.src.source.label == "SLOT2")
    assert twin.dest.is_relative_to(dest / EVENT) and twin.recorded_sha
    assert delete(dplan).counts() == {DeleteOutcome.DELETED: 4}
    assert files_under(r1) == files_under(r2) == set()
    assert len(files_under(dest)) == 2


# ---- what is never deleted --------------------------------------------------------------------


def test_failed_copy_is_not_deleted(cards, dest):
    report = imported(cards, dest, ops=FaultyFileOps(read_error={"DSC_0003.NEF": 0}))
    dplan = engine.plan_deletion(report)
    assert excluded(dplan)["SLOT1:DSC_0003.NEF"] == "not deleted: the copy failed"
    delete(dplan)
    assert cards[0].find("DSC_0003.NEF").exists()


def test_cancelled_and_deselected_are_not_deleted(tmp_path, dest):
    shots = [*SHOTS[:1], FakeShot("DSC_0004", D(2026, 1, 24, 0, 15, 0)),
             FakeShot("DSC_0005", D(2026, 1, 23, 23, 50, 0))]
    r1, r2 = make_zf_cards(tmp_path / "c", shots, "raw_jpg", junk=False)
    token = CancelToken()

    def cancel_on(path: Path, n: int) -> None:
        if path.name == "DSC_0005.NEF":
            token.cancel()

    report = run_import([r1, r2], dest, day_start=time(0, 0), days=[DAY1],
                        ops=FaultyFileOps(on_chunk=cancel_on), cancel=token)
    assert report.status == "cancelled"
    reasons = excluded(engine.plan_deletion(report))
    assert reasons["SLOT1:DSC_0004.NEF"] == "not deleted: its day was not imported"
    assert reasons["SLOT1:DSC_0005.NEF"] == "not deleted: not copied (import cancelled)"
    assert reasons["SLOT2:DSC_0005.JPG"] == "not deleted: not copied (import cancelled)"
    assert DAY2 not in {i.capture.local.date() for i in report.plan.to_copy}


def test_culled_file_is_not_deleted(cards, dest):
    imported(cards, dest)
    (dest / EVENT / "RAW" / "DSC_0001.NEF").unlink()
    dplan = engine.plan_deletion(imported(cards, dest))
    assert excluded(dplan)["SLOT1:DSC_0001.NEF"].startswith("not deleted: no copy in the library")


def _flip_byte(path: Path, keep_time: bool) -> None:
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    if keep_time:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_each_doubt_keeps_the_card_file(cards, dest):
    """One import, then a different problem for each of the five card files."""
    dplan = engine.plan_deletion(imported(cards, dest))
    nef1, nef3 = cards[0].find("DSC_0001.NEF"), cards[0].find("DSC_0003.NEF")
    jpg1, jpg2, jpg3 = (cards[1].find(f"DSC_000{n}.JPG") for n in (1, 2, 3))
    os.utime(nef1, ns=(0, nef1.stat().st_mtime_ns + 4_000_000_000))      # touched on the card
    _flip_byte(nef3, keep_time=True)                                     # card bit rot
    _flip_byte(dest / EVENT / "JPG" / "DSC_0001.JPG", keep_time=True)    # library copy edited
    (dest / EVENT / "JPG" / "DSC_0002.JPG").unlink()                     # library copy gone
    os.chmod(jpg3, stat.S_IREAD)                                         # protected since

    dreport = delete(dplan)
    assert dreport.status == "ok" and dreport.bytes_freed == 0
    assert outcome_of(dreport, "DSC_0001.NEF") == (DeleteOutcome.KEPT,
                                                   "changed on the card since the import")
    outcome, reason = outcome_of(dreport, "DSC_0003.NEF")
    assert outcome is DeleteOutcome.KEPT and "does not match" in reason
    outcome, reason = outcome_of(dreport, "DSC_0001.JPG")
    assert outcome is DeleteOutcome.KEPT and "does not match" in reason
    assert outcome_of(dreport, "DSC_0002.JPG") == (DeleteOutcome.KEPT,
                                                   "the copy is missing from the library")
    assert outcome_of(dreport, "DSC_0003.JPG") == (DeleteOutcome.KEPT, "protected in the camera")
    assert all(p.exists() for p in (nef1, nef3, jpg1, jpg2, jpg3))
    with Ledger.open() as led:
        assert not any(led.get(c.ledger_id).deleted_at for c in dplan.candidates)


def test_more_doubts_keep_the_card_file(cards, dest):
    dplan = engine.plan_deletion(imported(cards, dest))
    by_name = {c.src.name: i for i, c in enumerate(dplan.candidates)}
    with open(dest / EVENT / "RAW" / "DSC_0003.NEF", "ab") as f:          # copy grew
        f.write(b"more")
    cards[1].find("DSC_0001.JPG").unlink()                               # gone from the card
    i = by_name["DSC_0002.JPG"]                                          # import-time hash differs
    dplan.candidates[i] = dataclasses.replace(dplan.candidates[i], recorded_sha="0" * 64)

    dreport = delete(dplan)
    assert outcome_of(dreport, "DSC_0003.NEF") == (DeleteOutcome.KEPT,
                                                   "the copy has a different size")
    assert outcome_of(dreport, "DSC_0001.JPG") == (DeleteOutcome.KEPT,
                                                   "already gone from the card")
    outcome, reason = outcome_of(dreport, "DSC_0002.JPG")
    assert outcome is DeleteOutcome.KEPT and "does not match" in reason
    assert cards[1].find("DSC_0002.JPG").exists()
    assert outcome_of(dreport, "DSC_0001.NEF")[0] is DeleteOutcome.DELETED
    assert outcome_of(dreport, "DSC_0003.JPG")[0] is DeleteOutcome.DELETED


def test_online_only_library_copy_is_kept(cards, dest, monkeypatch):
    dplan = engine.plan_deletion(imported(cards, dest))
    monkeypatch.setattr(deleter, "is_online_only", lambda st: True)
    dreport = delete(dplan)
    assert dreport.counts() == {DeleteOutcome.KEPT: 5}
    assert all("online-only" in r.reason for r in dreport.results)
    assert all(c.src.path.exists() for c in dplan.candidates)


def test_card_file_already_gone(cards, dest):
    dplan = engine.plan_deletion(imported(cards, dest))
    target = dplan.candidates[0].src.path
    target.unlink()
    dreport = delete(dplan)
    assert dreport.results[0].outcome is DeleteOutcome.KEPT
    assert dreport.results[0].reason == "already gone from the card"


def test_remove_failure_is_reported(cards, dest):
    class NoRemove(RealFileOps):
        def remove(self, p: Path) -> None:
            raise PermissionError(13, "Access is denied", str(p))

    dplan = engine.plan_deletion(imported(cards, dest))
    dreport = delete(dplan, ops=NoRemove())
    assert dreport.status == "partial"
    assert dreport.counts() == {DeleteOutcome.FAILED: 5}
    assert all(c.src.path.exists() for c in dplan.candidates)


# ---- confirmation, cancel, lock ---------------------------------------------------------------


def test_not_confirmed_raises_and_deletes_nothing(cards, dest):
    dplan = engine.plan_deletion(imported(cards, dest))
    before = [c.snapshot() for c in cards]
    with pytest.raises(ValueError):
        engine.delete_originals(dplan, confirmed=False)
    with pytest.raises(ValueError):
        deleter.delete_originals(dplan, confirmed=False)
    assert [c.snapshot() for c in cards] == before


def test_cancel_between_files(cards, dest):
    dplan = engine.plan_deletion(imported(cards, dest))
    token = CancelToken()

    def emit(event: object) -> None:
        if isinstance(event, FileDone):
            token.cancel()

    dreport = delete(dplan, cancel=token, emit=emit)
    assert dreport.status == "cancelled"
    assert [r.outcome for r in dreport.results] == [DeleteOutcome.DELETED] + \
        [DeleteOutcome.CANCELLED] * 4
    assert all(c.src.path.exists() for c in dplan.candidates[1:])


def test_delete_holds_the_import_lock(cards, dest):
    dplan = engine.plan_deletion(imported(cards, dest))
    with engine.import_lock(), pytest.raises(AnotherImportRunning):
        delete(dplan)
    assert all(c.src.path.exists() for c in dplan.candidates)


def test_nothing_to_delete_after_a_failed_import(cards, dest):
    report = imported(cards, dest, ops=FaultyFileOps(read_error={"*": 0}))
    dplan = engine.plan_deletion(report)
    assert dplan.candidates == [] and dplan.bytes == 0
    assert delete(dplan).results == []


def test_same_card_given_twice_deletes_each_file_once(cards, dest):
    root = cards[0].root
    report = run_import([root, root], dest)
    dplan = engine.plan_deletion(report)
    assert len(dplan.candidates) == 4          # 2 unprotected NEFs, seen through both sources
    dreport = delete(dplan)
    assert dreport.counts() == {DeleteOutcome.DELETED: 2, DeleteOutcome.KEPT: 2}
    assert {r.reason for r in dreport.results if r.outcome is DeleteOutcome.KEPT} == {
        "already gone from the card"}
    assert len(files_under(dest)) == 3          # the protected NEF was copied too
