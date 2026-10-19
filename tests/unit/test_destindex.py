"""DestIndex: listings, "same file" decisions and stem owners in a destination event folder."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
from fakes.files import mtime_ns, write_file
from fakes.jpeg import make_jpeg
from fakes.raw import make_nef

from raw_jpg_import import destindex
from raw_jpg_import.config import load_config
from raw_jpg_import.destindex import DestFile, DestIndex, library_stem
from raw_jpg_import.grouping import EventTarget
from raw_jpg_import.ledger import Ledger, Row, root_key
from raw_jpg_import.metadata import read_capture
from raw_jpg_import.models import Capture, Source, SourceFile
from raw_jpg_import.scanner import make_file

T = datetime(2026, 1, 23, 23, 2, 10)
EVENT = "2026.01.23 - Sunset"


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "lib"
    (r / EVENT).mkdir(parents=True)
    return r


@pytest.fixture
def target(root) -> EventTarget:
    return EventTarget(path=root / EVENT, folder=EVENT, exists=True, days=frozenset({T.date()}))


@pytest.fixture
def card(tmp_path) -> Source:
    return Source(root=tmp_path / "card", label="CARD", kind="card")


def card_file(src: Source, name: str, data: bytes, when: datetime) -> tuple[SourceFile, Capture]:
    path = write_file(src.root / "DCIM/100NCZ_F" / name, data, when)
    f = make_file(src, path, load_config(use_user=False).formats, os.stat(path))
    return f, read_capture(path)


class CountingReader:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, path: Path) -> Capture:
        self.calls.append(path)
        return read_capture(path)


# ---- listings ---------------------------------------------------------------------------------


def test_listing_keys_are_casefolded_and_only_files(root, target):
    write_file(target.path / "RAW/DSC_0001.NEF", b"a")
    write_file(target.path / "RAW/dsc_0002.nef", b"bb")
    (target.path / "RAW/SUB").mkdir()
    idx = DestIndex(root)
    listing = idx.listing(target, "RAW")
    assert set(listing) == {"dsc_0001.nef", "dsc_0002.nef"}
    assert listing["dsc_0002.nef"].path.name == "dsc_0002.nef"
    assert listing["dsc_0002.nef"].size == 2


def test_listing_of_missing_folder_or_new_target_is_empty(root, target):
    write_file(root / "2026.01.24/RAW/DSC_0001.NEF", b"x")
    idx = DestIndex(root)
    assert idx.listing(target, "JPG") == {}
    new = EventTarget(path=root / "2026.01.24", folder="2026.01.24", exists=False,
                      days=frozenset())
    assert idx.listing(new, "RAW") == {}          # not marked existing: never listed


def test_listing_is_cached(root, target):
    idx = DestIndex(root)
    assert idx.listing(target, "RAW") == {}
    write_file(target.path / "RAW/DSC_0001.NEF", b"a")
    assert idx.listing(target, "RAW") == {}
    assert DestIndex(root).listing(target, "RAW")


def test_subfolders(root, target):
    for name in ("RAW", "JPG", "MOV"):
        (target.path / name).mkdir()
    write_file(target.path / "notes.txt", b"x")
    assert DestIndex(root).subfolders(target) == ["JPG", "MOV", "RAW"]


@pytest.mark.parametrize("name, stem", [
    ("DSC_0001.NEF", "dsc_0001"),
    ("DSC_0001-2.NEF", "dsc_0001-2"),
    ("DSC_0001-v2.JPG", "dsc_0001"),
    ("dsc_0001-V3.jpg", "dsc_0001"),
    ("DSC_0001.NEF.xmp", "dsc_0001"),
    ("DSC_0001-2-v2.JPG", "dsc_0001-2"),
    ("_DSC0005.NEF", "_dsc0005"),
    ("README", "readme"),
])
def test_library_stem(name, stem):
    assert library_stem(name) == stem


# ---- same_file --------------------------------------------------------------------------------


def _dest(target: EventTarget, rel: str, data: bytes, when: datetime) -> DestFile:
    p = write_file(target.path / rel, data, when)
    st = p.stat()
    return DestFile(p, st.st_size, st.st_mtime_ns, False)


def test_same_file_by_size_and_mtime(root, target, card):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    idx = DestIndex(root)
    assert idx.same_file(_dest(target, "RAW/DSC_0001.NEF", data, T), f, cap, target)


def test_same_file_within_fat_tolerance(root, target, card):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    d = _dest(target, "RAW/DSC_0001.NEF", data, T)
    near = DestFile(d.path, d.size, d.mtime_ns + 2_000_000_000, False)
    far = DestFile(d.path, d.size, d.mtime_ns + 2_000_000_001, False)
    reader = CountingReader()
    idx = DestIndex(root, read_capture=reader)
    assert idx.same_file(near, f, cap, target)
    assert reader.calls == []
    assert idx.same_file(far, f, cap, target)        # by EXIF
    assert reader.calls == [d.path]


def test_size_differs_is_never_the_same(root, target, card):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    d = _dest(target, "RAW/DSC_0001.NEF", data + b"!", T)
    assert not DestIndex(root).same_file(d, f, cap, target)


def test_same_file_by_exif_when_mtime_differs(root, target, card):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    d = _dest(target, "RAW/DSC_0001.NEF", data, datetime(2026, 2, 1, 9, 0, 0))
    assert DestIndex(root).same_file(d, f, cap, target)
    assert DestIndex(root).same_file(d, f, cap)            # no target: read directly


def test_other_exif_time_is_a_different_file(root, target, card):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    other = make_nef(datetime(2026, 1, 23, 18, 0, 0), name="a", subsec="12")
    assert len(other) == len(data)
    d = _dest(target, "RAW/DSC_0001.NEF", other, datetime(2026, 2, 1, 9, 0, 0))
    assert not DestIndex(root).same_file(d, f, cap, target)


def test_other_subsec_is_a_different_file(root, target, card):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    other = make_nef(T, name="a", subsec="47")
    d = _dest(target, "RAW/DSC_0001.NEF", other, datetime(2026, 2, 1, 9, 0, 0))
    assert not DestIndex(root).same_file(d, f, cap, target)


def test_inexact_card_capture_needs_matching_mtime(root, target, card):
    data = b"no metadata in here" * 100
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    assert not cap.exact
    d = _dest(target, "RAW/DSC_0001.NEF", data, datetime(2026, 2, 1, 9, 0, 0))
    assert not DestIndex(root).same_file(d, f, cap, target)


def test_online_only_with_other_mtime_is_not_the_same(root, target, card, monkeypatch):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    _dest(target, "RAW/DSC_0001.NEF", data, datetime(2026, 2, 1, 9, 0, 0))
    monkeypatch.setattr(destindex, "is_online_only", lambda st: True)
    reader = CountingReader()
    idx = DestIndex(root, read_capture=reader)
    d = idx.listing(target, "RAW")["dsc_0001.nef"]
    assert d.online_only
    assert not idx.same_file(d, f, cap, target)
    assert reader.calls == []                       # a placeholder is never downloaded


def test_online_only_with_same_mtime_is_the_same(root, target, card, monkeypatch):
    data = make_nef(T, name="a", subsec="12")
    f, cap = card_file(card, "DSC_0001.NEF", data, T)
    _dest(target, "RAW/DSC_0001.NEF", data, T)
    monkeypatch.setattr(destindex, "is_online_only", lambda st: True)
    idx = DestIndex(root)
    assert idx.same_file(idx.listing(target, "RAW")["dsc_0001.nef"], f, cap, target)


# ---- captures and stem owners -----------------------------------------------------------------


def _row(rel: str, capture: datetime, subsec: int | None = None) -> Row:
    return Row(id=None, session="s", kind="copied", dest_root="", event=EVENT, dest_rel=rel,
               orig_key=PurePosixPath(rel).name.casefold(), size=1, mtime_ns=0,
               capture=capture.isoformat(), capture_exact=True, subsec=subsec,
               shot_stem_key="dsc_0001", sha256=None, card_label="CARD", card_serial=None,
               src_rel="DCIM/100NCZ_F/" + PurePosixPath(rel).name)


def test_capture_from_ledger_beats_reading(root, target):
    d = _dest(target, "RAW/DSC_0001.NEF", make_nef(T, subsec="12"), T)
    ledger = Ledger.memory()
    row = _row("RAW/DSC_0001.NEF", datetime(2026, 1, 23, 18, 0, 0), 50)
    row.dest_root = root_key(root)
    ledger.record(row)
    reader = CountingReader()
    cap = DestIndex(root, ledger, read_capture=reader).capture_of(d, target)
    assert cap is not None and cap.source == "ledger" and cap.exact
    assert cap.local == datetime(2026, 1, 23, 18, 0, 0) and cap.subsec_ms == 50
    assert reader.calls == []


def test_capture_read_once_and_cached(root, target):
    d = _dest(target, "RAW/DSC_0001.NEF", make_nef(T, subsec="12"), T)
    reader = CountingReader()
    idx = DestIndex(root, Ledger.memory(), read_capture=reader)
    a = idx.capture_of(d, target)
    b = idx.capture_of(d, target)
    assert a == b and a is not None and a.exact and a.local == T
    assert reader.calls == [d.path]


def test_unreadable_capture_is_none(root, target):
    d = _dest(target, "RAW/DSC_0001.NEF", b"x", T)

    def broken(path: Path) -> Capture:
        raise OSError("gone")

    assert DestIndex(root, read_capture=broken).capture_of(d, target) is None


def test_stem_owners_cover_every_subfolder(root, target, monkeypatch):
    when = {"RAW/DSC_0001.NEF": T, "JPG/DSC_0001.JPG": T, "JPG/DSC_0001-v2.JPG": T,
            "MOV/dsc_0001.mov": T, "RAW/DSC_0001-2.NEF": datetime(2026, 1, 23, 23, 40),
            "OTHER/NOTES.XYZ": T, "RAW/DSC_00010.NEF": T}
    for rel, t in when.items():
        _dest(target, rel, make_jpeg(t, name=rel, subsec="12"), t)
    idx = DestIndex(root)
    owners = idx.stem_owners(target, "dsc_0001")
    assert sorted(p.relative_to(target.path).as_posix() for p, _ in owners) == [
        "JPG/DSC_0001-v2.JPG", "JPG/DSC_0001.JPG", "MOV/dsc_0001.mov", "RAW/DSC_0001.NEF"]
    assert [p.relative_to(target.path).as_posix() for p, _ in
            idx.stem_owners(target, "dsc_0001-2")] == ["RAW/DSC_0001-2.NEF"]
    assert idx.stem_owners(target, "dsc_9999") == []


def test_stem_owner_online_only_has_no_capture(root, target, monkeypatch):
    _dest(target, "RAW/DSC_0001.NEF", make_nef(T, subsec="12"), datetime(2026, 2, 1))
    monkeypatch.setattr(destindex, "is_online_only", lambda st: True)
    reader = CountingReader()
    owners = DestIndex(root, read_capture=reader).stem_owners(target, "dsc_0001")
    assert [cap for _, cap in owners] == [None]
    assert reader.calls == []


def test_mtime_helper_matches_written_files(tmp_path):
    p = write_file(tmp_path / "x", b"x", T)
    assert p.stat().st_mtime_ns == mtime_ns(T)
