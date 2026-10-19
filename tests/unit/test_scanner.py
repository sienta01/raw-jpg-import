from __future__ import annotations

import dataclasses
import os
import stat
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

from raw_jpg_import import scanner
from raw_jpg_import.errors import Cancelled
from raw_jpg_import.events import CancelToken, Progress
from raw_jpg_import.models import Source
from raw_jpg_import.scanner import make_file, scan_source

DIR = "DCIM/100NCZ_F"


def _write(p: Path, data: bytes = b"x" * 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "NIKON Z F"
    d = root / DIR
    _write(d / "DSC_0001.NEF", b"n" * 100)
    _write(d / "DSC_0001.JPG", b"j" * 50)
    _write(d / "DSC_0002.MOV", b"m" * 70)
    _write(d / "DSC_0002.WAV", b"w" * 5)
    _write(d / "DSC_0003.XYZ", b"?" * 3)
    _write(d / "DSC_0004.JPG", b"")
    protected = _write(d / "DSC_0005.NEF", b"r" * 20)
    _write(d / "._DSC_0001.NEF")
    _write(d / "DSC_0006.NEF.part")
    _write(root / "DCIM" / "NIKON001.DSC")
    _write(root / "NC_FLLST.DAT")
    _write(root / ".hidden")
    _write(root / "README")
    _write(root / "$RECYCLE.BIN" / "x.jpg")
    _write(root / "System Volume Information" / "y")
    _write(root / ".Trashes" / "z.NEF")
    _write(root / "PRIVATE/M4ROOT/CLIP/C0001.MP4", b"v" * 30)
    _write(root / "PRIVATE/M4ROOT/CLIP/C0001M01.XML", b"<x/>")
    _write(root / "PRIVATE/M4ROOT/THMBNL/C0001T01.JPG")
    os.chmod(protected, stat.S_IREAD)
    yield root
    os.chmod(protected, stat.S_IREAD | stat.S_IWRITE)


@pytest.fixture
def src(card):
    return Source(root=card, label="NIKON Z F", kind="card")


EXPECTED = [
    f"{DIR}/DSC_0001.JPG",
    f"{DIR}/DSC_0001.NEF",
    f"{DIR}/DSC_0002.MOV",
    f"{DIR}/DSC_0002.WAV",
    f"{DIR}/DSC_0003.XYZ",
    f"{DIR}/DSC_0004.JPG",
    f"{DIR}/DSC_0005.NEF",
    "PRIVATE/M4ROOT/CLIP/C0001.MP4",
    "PRIVATE/M4ROOT/CLIP/C0001M01.XML",
    "README",
]


def test_scan_fake_card(src, card, fmt):
    scan = scan_source(src, fmt)
    assert [f.rel.as_posix() for f in scan.files] == EXPECTED
    assert scan.errors == []
    assert scan.ignored == Counter({"junk": 4, "dsc": 1})
    assert scan.unknown == {"xyz": [card / DIR / "DSC_0003.XYZ"], "": [card / "README"]}

    by_rel = {f.rel.as_posix(): f for f in scan.files}
    cats = {rel: (f.category.key if f.category else None) for rel, f in by_rel.items()}
    assert cats == {
        f"{DIR}/DSC_0001.JPG": "jpg", f"{DIR}/DSC_0001.NEF": "raw",
        f"{DIR}/DSC_0002.MOV": "video", f"{DIR}/DSC_0002.WAV": "sidecar",
        f"{DIR}/DSC_0003.XYZ": None, f"{DIR}/DSC_0004.JPG": "jpg",
        f"{DIR}/DSC_0005.NEF": "raw", "PRIVATE/M4ROOT/CLIP/C0001.MP4": "video",
        "PRIVATE/M4ROOT/CLIP/C0001M01.XML": "sidecar", "README": None,
    }

    nef = by_rel[f"{DIR}/DSC_0001.NEF"]
    path = card / DIR / "DSC_0001.NEF"
    assert nef.source == src
    assert nef.path == path
    assert nef.rel == PurePosixPath(DIR, "DSC_0001.NEF")
    assert (nef.name, nef.stem, nef.ext, nef.orig_ext) == ("DSC_0001.NEF", "DSC_0001", "nef", "NEF")
    assert nef.size == 100
    assert nef.mtime_ns == os.stat(path).st_mtime_ns
    assert by_rel["README"].ext == "" and by_rel["README"].stem == "README"

    assert [rel for rel, f in by_rel.items() if f.readonly] == [f"{DIR}/DSC_0005.NEF"]
    assert len(scan.warnings) == 1 and f"{DIR}/DSC_0004.JPG" in scan.warnings[0]
    assert by_rel[f"{DIR}/DSC_0004.JPG"].size == 0
    assert scan.total_bytes == 100 + 50 + 70 + 5 + 3 + 0 + 20 + 30 + 4 + 10


def test_make_file_matches_scan(src, card, fmt):
    scan = scan_source(src, fmt)
    for f in scan.files:
        assert make_file(src, f.path, fmt, os.stat(f.path)) == f


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters")
def test_drive_root_source(card, fmt):
    st = os.stat(card / DIR / "DSC_0001.NEF")
    for root in (Path("X:/"), Path("X:")):
        src = Source(root=root, label="X")
        f = make_file(src, Path("X:/DCIM/100NCZ_F/DSC_0001.NEF"), fmt, st)
        assert f.rel == PurePosixPath(DIR, "DSC_0001.NEF")
    assert scanner._top_dir(Path("X:")) == Path("X:\\")
    assert scanner._top_dir(Path("X:\\")) == Path("X:\\")
    assert scanner._top_dir(Path("X:sub")) == Path("X:sub")


def test_dot_and_part_files_always_junk(src, fmt):
    bare = dataclasses.replace(fmt, skip_files=())
    scan = scan_source(src, bare)
    names = {f.name for f in scan.files}
    assert {"._DSC_0001.NEF", ".hidden", "DSC_0006.NEF.part"}.isdisjoint(names)
    # NC_FLLST.DAT is no longer junk, so it counts as an ignore-role .dat file.
    assert scan.ignored == Counter({"junk": 3, "dsc": 1, "dat": 1})


def test_progress_events(src, fmt):
    events: list[object] = []
    scan = scan_source(src, fmt, emit=events.append)
    progress = [e for e in events if isinstance(e, Progress)]
    assert progress and all(p.phase == "scan" and p.files_total == 0 for p in progress)
    assert progress[0].current in EXPECTED   # throttled: the first file always reports
    assert progress[-1].files_done == len(scan.files)


def test_cancel_before_start(src, fmt):
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        scan_source(src, fmt, cancel=token)


def test_cancel_during_scan(src, fmt):
    token = CancelToken()

    def emit(event: object) -> None:
        token.cancel()

    with pytest.raises(Cancelled):
        scan_source(src, fmt, cancel=token, emit=emit)


def test_cancel_inside_big_directory(tmp_path, fmt):
    for i in range(scanner.CANCEL_EVERY + 5):
        _write(tmp_path / f"DSC_{i:04d}.NEF", b"")
    token = CancelToken()
    seen: list[str] = []

    def emit(event: object) -> None:
        if isinstance(event, Progress) and event.current:
            seen.append(event.current)
            token.cancel()

    with pytest.raises(Cancelled):
        scan_source(Source(tmp_path, "big"), fmt, cancel=token, emit=emit)
    assert seen  # cancelled inside the only directory, before the walk ended


def test_permission_error_is_collected(src, card, fmt, monkeypatch):
    blocked = card / "PRIVATE" / "M4ROOT" / "CLIP"
    real = os.scandir

    def scandir(path="."):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(blocked)):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    scan = scan_source(src, fmt)
    assert scan.errors == [(blocked, "Permission denied")]
    assert [f.rel.as_posix() for f in scan.files] == [
        r for r in EXPECTED if not r.startswith("PRIVATE/")]


def test_missing_root(tmp_path, fmt):
    scan = scan_source(Source(tmp_path / "gone", "gone"), fmt)
    assert scan.files == [] and len(scan.errors) == 1
    assert scan.errors[0][0] == tmp_path / "gone"


def test_folder_loop_is_not_followed(tmp_path, fmt):
    root = tmp_path / "src"
    _write(root / DIR / "DSC_0001.NEF")
    loop = root / DIR / "loop"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(root), str(loop))
    else:
        try:
            os.symlink(root, loop, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create symlinks here")
    try:
        scan = scan_source(Source(root, "src"), fmt)
    finally:
        if sys.platform == "win32":
            os.rmdir(loop)      # removes the junction only
        else:
            os.unlink(loop)
    assert [f.rel.as_posix() for f in scan.files] == [f"{DIR}/DSC_0001.NEF"]
    # Neither a symlink nor a Windows junction is ever followed.
    assert scan.ignored == Counter({"symlink": 1})
    assert scan.warnings == []


def test_readonly_from_stat_mode(tmp_path):
    p = _write(tmp_path / "a.NEF")
    os.chmod(p, stat.S_IREAD)
    try:
        assert scanner.is_readonly(os.stat(p))
    finally:
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    assert not scanner.is_readonly(os.stat(p))
