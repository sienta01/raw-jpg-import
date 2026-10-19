"""Regression tests for reviewed safety defects (scanner, deleter, fsops, copier, config).

Every card here is a fake folder under ``tmp_path``; card detection is blocked.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes.card import FakeCard, files_under, make_plan, no_real_cards, sha256_file

from raw_jpg_import import engine, fsops
from raw_jpg_import.deleter import DeleteCandidate, DeleteOutcome, DeletionPlan, delete_originals
from raw_jpg_import.fsops import RealFileOps, is_within
from raw_jpg_import.models import Source
from raw_jpg_import.scanner import make_file, scan_source

WHEN = datetime(2026, 1, 23, 10, 0, 0)
WINDOWS = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _no_real_cards(monkeypatch):
    no_real_cards(monkeypatch)


def _junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@contextlib.contextmanager
def dir_link(target: Path, link: Path):
    """A junction (Windows) or a directory symlink (elsewhere) at ``link``; removed after."""
    if WINDOWS:
        _junction(target, link)
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create symlinks here")
    try:
        yield link
    finally:
        if WINDOWS:
            os.rmdir(link)          # removes the junction only
        else:
            os.unlink(link)


def _one_candidate(fmt, card_root: Path, card_file: Path, dest: Path) -> DeletionPlan:
    src = Source(root=card_root, label="CARD", kind="card")
    sf = make_file(src, card_file, fmt, os.stat(card_file))
    return DeletionPlan(candidates=[DeleteCandidate(0, sf, dest, None, None)], excluded=[],
                        bytes=sf.size, per_source={"CARD": (1, sf.size)})


# ---- finding 0: the "card file" and its copy are one file -----------------------------------


def test_delete_keeps_a_card_file_that_is_a_hard_link_of_its_copy(tmp_path, fmt):
    lib = tmp_path / "lib" / "RAW" / "DSC_0001.NEF"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"only copy" * 100)
    card_file = tmp_path / "card" / "DCIM" / "DSC_0001.NEF"
    card_file.parent.mkdir(parents=True)
    try:
        os.link(lib, card_file)
    except OSError:
        pytest.skip("no hard links here")
    dplan = _one_candidate(fmt, tmp_path / "card", card_file, lib)
    dreport = delete_originals(dplan, confirmed=True)
    assert [(r.outcome, r.reason) for r in dreport.results] == [
        (DeleteOutcome.KEPT, "the original and the copy are the same file")]
    assert lib.read_bytes() == b"only copy" * 100 and card_file.exists()


def test_delete_keeps_a_file_spelled_two_ways_without_file_ids(tmp_path, fmt):
    """Filesystems with no inode numbers: the path comparison still recognises one file."""
    lib = tmp_path / "lib" / "DSC_0001.NEF"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"x" * 300)
    other_spelling = tmp_path / "lib" / ".." / "lib" / "DSC_0001.NEF"

    class NoInodes(RealFileOps):
        def stat(self, p: Path):
            st = os.stat(p)
            return SimpleNamespace(**{k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
                                   | {"st_ino": 0, "st_dev": 0})

    dplan = _one_candidate(fmt, tmp_path / "lib", lib, other_spelling)
    dreport = delete_originals(dplan, confirmed=True, ops=NoInodes())
    assert dreport.results[0].outcome is DeleteOutcome.KEPT
    assert lib.exists()


def test_scanner_does_not_descend_into_a_junction(tmp_path, fmt):
    elsewhere = tmp_path / "library" / "RAW"
    elsewhere.mkdir(parents=True)
    (elsewhere / "DSC_0009.NEF").write_bytes(b"n" * 50)
    card = FakeCard(tmp_path / "card")
    card.photo("DSC_0001.NEF", WHEN)
    with dir_link(tmp_path / "library", card.root / "DCIM" / "linked"):
        scan = scan_source(Source(card.root, "card"), fmt)
    assert [f.name for f in scan.files] == ["DSC_0001.NEF"]
    assert scan.ignored == Counter({"symlink": 1})


def test_folder_source_with_a_link_to_the_library_never_deletes_the_library(tmp_path):
    card = FakeCard(tmp_path / "card", "NIKON Z F")
    card.photo("DSC_0001.NEF", WHEN, subsec="11")
    card.photo("DSC_0001.JPG", WHEN, subsec="11")
    lib = tmp_path / "lib"
    lib.mkdir()
    first = engine.execute(make_plan([card.root], lib, name="Trip"))
    assert first.status == "ok"
    library = {p: sha256_file(lib / p) for p in files_under(lib)}
    assert len(library) == 2
    for p in list(card.root.rglob("*.*")):     # the card is formatted later
        p.unlink()
    pictures = tmp_path / "Pictures"
    pictures.mkdir()
    with dir_link(lib, pictures / "linked"):
        plan = make_plan([pictures], lib, name="Trip")
        assert not [i for i in plan.items if "linked" in i.src.rel.parts]
        if not plan.problems:
            report = engine.execute(plan)
            engine.delete_originals(engine.plan_deletion(report), confirmed=True)
    assert {p: sha256_file(lib / p) for p in files_under(lib)} == library


# ---- finding 18: the destination is on the card, spelled another way ------------------------


def _long_path(p: Path) -> Path:
    return Path("\\\\?\\" + str(p))


def _admin_share(p: Path) -> Path:
    return Path("\\\\localhost\\" + str(p)[0] + "$" + str(p)[2:])


@pytest.mark.skipif(not WINDOWS, reason="Windows path spellings")
def test_is_within_sees_other_spellings_of_the_same_folder(tmp_path):
    card = tmp_path / "card"
    (card / "Photos").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    forms = [_long_path(card / "Photos")]
    share = _admin_share(card / "Photos")
    if share.is_dir():
        forms.append(share)
    for dest in forms:
        assert is_within(dest, card), dest
        assert is_within(card / "Photos", dest), dest
        assert not is_within(card, dest), dest
    assert not is_within(_long_path(tmp_path / "other"), card)
    assert is_within(_long_path(card), card) and is_within(card, _long_path(card))


@pytest.mark.skipif(not WINDOWS, reason="Windows path spellings")
def test_plan_refuses_a_destination_on_the_card_spelled_another_way(tmp_path):
    card = FakeCard(tmp_path / "card", "NIKON Z F")
    card.photo("DSC_0001.NEF", WHEN, subsec="11")
    (card.root / "Photos").mkdir()
    plan = make_plan([card.root], _long_path(card.root / "Photos"), name="Trip",
                     probe_write=True)
    assert any("inside each other" in p for p in plan.problems), plan.problems
    assert files_under(card.root) == {"DCIM/100NCZ_F/DSC_0001.NEF"}     # no write probe


def test_is_within_by_identity_when_the_case_differs(tmp_path, monkeypatch):
    """Linux compares paths case-sensitively, but a FAT card ignores case."""
    (tmp_path / "CARD" / "DCIM" / "backup").mkdir(parents=True)
    other_case = tmp_path / "CARD" / "dcim" / "backup"
    if not other_case.is_dir():
        pytest.skip("case-sensitive filesystem")
    monkeypatch.setattr(fsops, "_platform", lambda: "linux")
    # Path.resolve on Linux keeps the spelling it was given.
    monkeypatch.setattr(fsops, "_resolved", lambda p: Path(os.path.abspath(p)))
    assert is_within(other_case, tmp_path / "CARD" / "DCIM")
    assert is_within(tmp_path / "CARD" / "DCIM", tmp_path / "CARD" / "dcim")
    assert not is_within(tmp_path / "CARD", other_case)


@pytest.mark.parametrize(("given", "expected"), [
    (r"\\?\C:\Photos", r"C:\Photos"),
    (r"\\?\unc\nas\share\x", r"\\nas\share\x"),
    ("//?/C:/Photos", "C:/Photos"),
    (r"\\.\D:" "\\", "D:\\"),
    (r"\\?\Volume{1234}\x", r"\\?\Volume{1234}\x"),
    (r"C:\Photos", r"C:\Photos"),
    ("/media/card", "/media/card"),
])
def test_long_path_prefix_is_stripped(given, expected):
    assert fsops._strip_long_prefix(given) == expected


# ---- finding 19: backup twins are compared by content ---------------------------------------


def _backup_pair(tmp_path, *, corrupt: bool) -> tuple[FakeCard, FakeCard]:
    """The same JPG on both cards; with ``corrupt`` card 1's copy has other bytes, same size."""
    c1 = FakeCard(tmp_path / "SLOT1", "NIKON Z F")
    c2 = FakeCard(tmp_path / "SLOT2", "NIKON Z F")
    for c in (c1, c2):
        c.photo("DSC_0001.JPG", WHEN, subsec="11", seed="shot-1")
        c.photo("DSC_0002.JPG", WHEN.replace(minute=5), subsec="22", seed="shot-2")
    if corrupt:
        bad = c1.find("DSC_0001.JPG")
        st = bad.stat()
        data = bytearray(bad.read_bytes())
        data[len(data) // 2: len(data) // 2 + 512] = bytes(512)
        bad.write_bytes(bytes(data))
        os.utime(bad, ns=(st.st_atime_ns, st.st_mtime_ns))
    return c1, c2


def _by_card(report, name: str) -> dict[str, object]:
    return {i.src.source.root.name: report.results[i.id] for i in report.plan.items
            if i.src.name == name}


def test_backup_twins_that_differ_are_both_kept(tmp_path):
    c1, c2 = _backup_pair(tmp_path, corrupt=True)
    lib = tmp_path / "lib"
    lib.mkdir()
    events: list[object] = []
    plan = make_plan([c1.root, c2.root], lib, name="Trip")
    report = engine.execute(plan, emit=events.append)
    assert report.status == "ok"
    jpg = lib / "2026.01.23 - Trip" / "JPG"
    assert sha256_file(jpg / "DSC_0001.JPG") == sha256_file(c1.find("DSC_0001.JPG"))
    assert sha256_file(jpg / "DSC_0001-v2.JPG") == sha256_file(c2.find("DSC_0001.JPG"))
    twin = _by_card(report, "DSC_0001.JPG")["SLOT2"]
    assert twin.outcome.name == "COPIED" and twin.dest == jpg / "DSC_0001-v2.JPG"
    assert "differs" in twin.error
    assert len(report.warnings) == 1 and "differ" in report.warnings[0]
    assert any(type(e).__name__ == "Notice" and "differ" in e.text for e in events)
    # The identical shot stays one file.
    assert _by_card(report, "DSC_0002.JPG")["SLOT2"].outcome.name == "DUPLICATE"
    assert sorted(files_under(jpg)) == ["DSC_0001-v2.JPG", "DSC_0001.JPG", "DSC_0002.JPG"]
    # Running again copies nothing and finds the -v2 copy.
    again = engine.execute(make_plan([c1.root, c2.root], lib, name="Trip"))
    assert again.bytes_copied == 0 and again.status == "ok"
    twin = _by_card(again, "DSC_0001.JPG")["SLOT2"]
    assert twin.outcome.name == "ALREADY_THERE" and twin.dest == jpg / "DSC_0001-v2.JPG"
    assert sorted(files_under(jpg)) == ["DSC_0001-v2.JPG", "DSC_0001.JPG", "DSC_0002.JPG"]
    # Both card files have a verified copy, so both may go.
    dreport = engine.delete_originals(engine.plan_deletion(again), confirmed=True)
    assert {r.outcome for r in dreport.results} == {DeleteOutcome.DELETED}
    assert sorted(files_under(jpg)) == ["DSC_0001-v2.JPG", "DSC_0001.JPG", "DSC_0002.JPG"]


def test_identical_backup_twins_are_hashed_once(tmp_path):
    from fakes.card import FaultyFileOps, forget_history

    c1, c2 = _backup_pair(tmp_path, corrupt=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    ops = FaultyFileOps()
    report = engine.execute(make_plan([c1.root, c2.root], lib, name="Trip"), ops=ops)
    twins = _by_card(report, "DSC_0001.JPG")
    assert twins["SLOT2"].outcome.name == "DUPLICATE"
    assert twins["SLOT2"].sha256 == twins["SLOT1"].sha256 == sha256_file(c2.find("DSC_0001.JPG"))
    assert {p.parent.parent.parent.name for p in ops.opened} == {"SLOT1", "SLOT2"}
    assert files_under(lib / "2026.01.23 - Trip") == {"JPG/DSC_0001.JPG", "JPG/DSC_0002.JPG"}
    # The history vouches for the twins: a re-run reads no card file.
    ops = FaultyFileOps()
    again = engine.execute(make_plan([c1.root, c2.root], lib, name="Trip"), ops=ops)
    assert ops.opened == [] and again.bytes_copied == 0
    assert all(r.outcome.name in ("ALREADY_THERE", "DUPLICATE") for r in again.results.values())
    assert all(r.sha256 for r in again.results.values())
    dplan = engine.plan_deletion(again)
    assert len(dplan.candidates) == 4 and all(c.recorded_sha for c in dplan.candidates)
    # Without the history the twins are compared again.
    forget_history()
    ops = FaultyFileOps()
    third = engine.execute(make_plan([c1.root, c2.root], lib, name="Trip"), ops=ops)
    assert {p.parent.parent.parent.name for p in ops.opened} == {"SLOT2"}
    assert _by_card(third, "DSC_0001.JPG")["SLOT2"].outcome.name == "DUPLICATE"


def test_twin_check_counts_in_progress_and_can_be_cancelled(tmp_path):
    from fakes.card import FaultyFileOps, cancel_after

    from raw_jpg_import.events import CancelToken, Progress

    c1, c2 = _backup_pair(tmp_path, corrupt=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    events: list[object] = []
    plan = make_plan([c1.root, c2.root], lib, name="Trip")
    report = engine.execute(plan, emit=events.append)
    last = [e for e in events if isinstance(e, Progress)][-1]
    twin_bytes = sum(i.src.size for i in plan.items if i.src.source.root.name == "SLOT2")
    assert last.bytes_done == last.bytes_total == plan.bytes_to_copy + twin_bytes
    assert last.files_done == last.files_total == 4
    assert report.status == "ok"

    lib2 = tmp_path / "lib2"
    lib2.mkdir()
    token = CancelToken()
    ops = FaultyFileOps(on_chunk=cancel_after(token, "SLOT2/DCIM/100NCZ_F/DSC_0001.JPG"))
    report = engine.execute(make_plan([c1.root, c2.root], lib2, name="Trip"), ops=ops,
                            cancel=token)
    assert report.status == "cancelled"
    twins = _by_card(report, "DSC_0001.JPG")
    assert twins["SLOT1"].outcome.name == "COPIED"
    assert twins["SLOT2"].outcome.name == "CANCELLED"
    assert _by_card(report, "DSC_0002.JPG")["SLOT2"].outcome.name == "CANCELLED"
    dplan = engine.plan_deletion(report)
    assert {c.src.source.root.name for c in dplan.candidates} == {"SLOT1"}


# ---- finding 32: unknown files and photos must not share a folder ---------------------------


@pytest.mark.parametrize("other", ["RAW", "raw", "JPG", "MOV"])
def test_other_folder_may_not_be_a_category_folder(tmp_path, other):
    from raw_jpg_import.config import load_config
    from raw_jpg_import.errors import ConfigError

    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[import]\nunknown = "copy"\nother_folder = "{other}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="other_folder"):
        load_config(cfg)


def test_new_category_may_not_use_the_other_folder(tmp_path):
    from raw_jpg_import.config import load_config
    from raw_jpg_import.errors import ConfigError

    cfg = tmp_path / "config.toml"
    cfg.write_text('[categories.proxy]\nfolder = "Other"\nextensions = ["lrv"]\n',
                   encoding="utf-8")
    with pytest.raises(ConfigError, match="other_folder"):
        load_config(cfg)
    cfg.write_text('[import]\nother_folder = "MISC"\n', encoding="utf-8")
    assert load_config(cfg).formats.other_folder == "MISC"


def test_a_name_taken_after_planning_is_compared_by_hash(tmp_path):
    """Same size and time is not enough to call a file already imported."""
    card = FakeCard(tmp_path / "card", "NIKON Z F")
    card.photo("DSC_0001.NEF", WHEN, subsec="11")
    memo = card.file("DCIM/100NCZ_F/DSC_0001.WAV", b"RIFF" + b"A" * 4000, WHEN)
    lib = tmp_path / "lib"
    lib.mkdir()
    plan = make_plan([card.root], lib, name="Trip")
    taken = lib / "2026.01.23 - Trip" / "RAW" / "DSC_0001.WAV"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"RIFF" + b"B" * 4000)
    st = memo.stat()
    os.utime(taken, ns=(st.st_atime_ns, st.st_mtime_ns))
    report = engine.execute(plan)
    res = next(report.results[i.id] for i in plan.items if i.src.name == "DSC_0001.WAV")
    assert res.outcome.name == "FAILED" and "appeared since planning" in res.error
    assert taken.read_bytes() == b"RIFF" + b"B" * 4000
    assert not [c for c in engine.plan_deletion(report).candidates if c.src.path == memo]


# ---- finding 16: a clip over 4 GiB on a FAT32 destination (Windows says "disk full") -------


@pytest.mark.parametrize(("free", "expected"), [(10 << 30, "too large"), (1 << 30, "full")])
def test_fat32_limit_reported_as_disk_full_fails_only_that_file(tmp_path, free, expected):
    import dataclasses

    from fakes.card import FaultyFileOps

    card = FakeCard(tmp_path / "card", "NIKON Z F")
    card.photo("DSC_0001.NEF", WHEN, subsec="11")
    clip = card.video("DSC_0010.MOV", WHEN.replace(minute=10))
    card.photo("DSC_0020.NEF", WHEN.replace(minute=20), subsec="22")
    lib = tmp_path / "lib"
    lib.mkdir()
    plan = make_plan([card.root], lib, name="Trip")
    item = next(i for i in plan.items if i.src.name == "DSC_0010.MOV")
    big = 5 << 30
    item.src = dataclasses.replace(item.src, size=big)

    class BigClipOnFat32(FaultyFileOps):
        def stat(self, p):
            st = os.stat(p)
            if Path(p) != clip:
                return st
            return SimpleNamespace(**{k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
                                   | {"st_size": big})

        def disk_free(self, p):
            return free

    report = engine.execute(plan, ops=BigClipOnFat32(enospc=["DSC_0010.MOV"]))
    res = report.results[item.id]
    others = [report.results[i.id].outcome.name for i in plan.items if i is not item]
    if expected == "too large":
        assert report.status == "partial" and report.abort_reason is None
        assert res.outcome.name == "FAILED" and "4 GB" in res.error
        assert others == ["COPIED", "COPIED"]
    else:                   # really full: stop, as before
        assert report.status == "aborted" and "full" in report.abort_reason
        assert others == ["COPIED", "CANCELLED"]
    assert not list(lib.rglob("*.part"))


# ---- finding 15: macOS shows a protected file on a FAT card as immutable --------------------


@pytest.mark.parametrize(("flags", "protected"), [
    (stat.UF_IMMUTABLE, True), (stat.SF_IMMUTABLE, True), (0, False), (stat.UF_HIDDEN, False)])
def test_immutable_flag_means_protected(flags, protected):
    from raw_jpg_import import scanner
    from raw_jpg_import.fsops import is_readonly

    st = SimpleNamespace(st_mode=stat.S_IFREG | 0o777, st_flags=flags, st_size=1, st_mtime_ns=0)
    assert is_readonly(st) is protected
    assert scanner.is_readonly(st) is protected


def test_delete_keeps_an_immutable_card_file(tmp_path, fmt):
    lib = tmp_path / "lib" / "DSC_0001.NEF"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"n" * 300)
    card_file = tmp_path / "card" / "DCIM" / "DSC_0001.NEF"
    card_file.parent.mkdir(parents=True)
    card_file.write_bytes(b"n" * 300)
    dplan = _one_candidate(fmt, tmp_path / "card", card_file, lib)

    class MacFat(RealFileOps):
        """What macOS reports for a camera-protected file on a FAT card."""

        def stat(self, p: Path):
            st = os.stat(p)
            fields = {k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
            fields.pop("st_file_attributes", None)
            if Path(p) == card_file:
                fields |= {"st_mode": stat.S_IFREG | 0o777, "st_flags": stat.UF_IMMUTABLE}
            return SimpleNamespace(**fields)

    dreport = delete_originals(dplan, confirmed=True, ops=MacFat())
    assert [(r.outcome, r.reason) for r in dreport.results] == [
        (DeleteOutcome.KEPT, "protected in the camera")]
    assert card_file.exists()


# ---- finding 25: no "insert a disk" dialog from the metadata pool or the deleter -----------


@contextlib.contextmanager
def _recording_guard(active: set[int]):
    import threading

    me = threading.get_ident()
    active.add(me)
    try:
        yield
    finally:
        active.discard(me)


def test_metadata_pool_threads_suppress_error_dialogs(tmp_path, fmt, monkeypatch):
    import threading

    from raw_jpg_import import metadata

    card = FakeCard(tmp_path / "card")
    for n in range(6):
        card.photo(f"DSC_000{n}.NEF", WHEN.replace(minute=n), subsec="11")
    files = scan_source(Source(card.root, "card"), fmt).files
    active: set[int] = set()
    calls: list[tuple[str, bool]] = []
    real = metadata.read_capture

    def spy(path, **kw):
        calls.append((threading.current_thread().name, threading.get_ident() in active))
        return real(path, **kw)

    metadata.clear_cache()
    monkeypatch.setattr(metadata, "no_error_dialogs", lambda: _recording_guard(active))
    monkeypatch.setattr(metadata, "read_capture", spy)
    metadata.read_many(files)
    assert len(calls) == 6
    assert all(name.startswith("rji-metadata") and guarded for name, guarded in calls)


@pytest.mark.skipif(not WINDOWS, reason="Windows thread error mode")
def test_metadata_pool_threads_really_set_the_error_mode(tmp_path, fmt, monkeypatch):
    import ctypes

    from raw_jpg_import import metadata

    k32 = ctypes.WinDLL("kernel32")
    k32.GetThreadErrorMode.restype = ctypes.c_uint
    card = FakeCard(tmp_path / "card")
    card.photo("DSC_0001.NEF", WHEN, subsec="11")
    files = scan_source(Source(card.root, "card"), fmt).files
    modes: list[int] = []
    real = metadata.read_capture

    def spy(path, **kw):
        modes.append(k32.GetThreadErrorMode())
        return real(path, **kw)

    metadata.clear_cache()
    monkeypatch.setattr(metadata, "read_capture", spy)
    metadata.read_many(files)
    assert modes and all(m & 0x0001 for m in modes)     # SEM_FAILCRITICALERRORS


def test_deleter_suppresses_error_dialogs(tmp_path, fmt, monkeypatch):
    import threading

    from raw_jpg_import import deleter

    lib = tmp_path / "lib" / "DSC_0001.NEF"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"n" * 300)
    card_file = tmp_path / "card" / "DCIM" / "DSC_0001.NEF"
    card_file.parent.mkdir(parents=True)
    card_file.write_bytes(b"n" * 300)
    active: set[int] = set()
    touched: list[bool] = []

    class Spy(RealFileOps):
        def stat(self, p):
            touched.append(threading.get_ident() in active)
            return super().stat(p)

        def remove(self, p):
            touched.append(threading.get_ident() in active)
            super().remove(p)

    monkeypatch.setattr(deleter, "no_error_dialogs", lambda: _recording_guard(active))
    dreport = delete_originals(_one_candidate(fmt, tmp_path / "card", card_file, lib),
                               confirmed=True, ops=Spy())
    assert dreport.results[0].outcome is DeleteOutcome.DELETED
    assert touched and all(touched)


# ---- finding 26: two cards with the same label are counted apart ----------------------------


def test_per_card_counts_keep_cards_with_the_same_label_apart(tmp_path):
    from fakes.card import FakeShot, default_cfg, make_zf_cards

    r1, r2 = make_zf_cards(tmp_path / "cards", [FakeShot("DSC_0001", WHEN)], "raw_jpg",
                           junk=False)
    cfg = default_cfg()
    scanned = [engine.scan_source(Source(r, "NIKON Z F", serial=f"000{i}-ABCD", kind="card",
                                         order=i), cfg) for i, r in enumerate((r1, r2))]
    lib = tmp_path / "lib"
    lib.mkdir()
    report = engine.execute(make_plan(engine.analyze(scanned), lib, name="Trip"))
    dplan = engine.plan_deletion(report)
    nef, jpg = (next(r.rglob(n)).stat().st_size for r, n in
                ((r1, "DSC_0001.NEF"), (r2, "DSC_0001.JPG")))
    assert dplan.per_source == {f"NIKON Z F ({r1})": (1, nef), f"NIKON Z F ({r2})": (1, jpg)}


def test_source_names():
    from raw_jpg_import.deleter import source_names

    a = Source(Path("D:/"), "NIKON Z F", "1111-1111", "card", 0)
    b = Source(Path("F:/"), "NIKON Z F", "2222-2222", "card", 1)
    c = Source(Path("C:/Photos/in"), "in", None, "folder", 2)
    again = Source(Path("D:/"), "NIKON Z F", "1111-1111", "card", 3)    # the same card twice
    names = source_names([a, b, c, again])
    assert names == {a.key: f"NIKON Z F ({a.root})", b.key: f"NIKON Z F ({b.root})",
                     c.key: "in"}
    assert source_names([a, again]) == {a.key: "NIKON Z F"}
