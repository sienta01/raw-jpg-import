"""Previews for the photo viewer: embedded raw JPEGs, decoding, and listing event folders."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from raw_jpg_import import previews

PIL = pytest.importorskip("PIL.Image")

from fakes.previews import fake_nef, jpeg_bytes  # noqa: E402  (needs Pillow)


@pytest.mark.parametrize("bo", ["<", ">"])
def test_embedded_jpeg_from_subifd(tmp_path, bo):
    big = jpeg_bytes(600, 400)
    small = jpeg_bytes(60, 40)
    p = tmp_path / "DSC_0001.NEF"
    p.write_bytes(fake_nef(big, orientation=6, bo=bo, thumb=small))
    data, orientation = previews.embedded_jpeg(p)
    assert data == big                       # the largest one wins
    assert orientation == 6


def test_embedded_jpeg_raf(tmp_path):
    jpg = jpeg_bytes(300, 200)
    head = b"FUJIFILMCCD-RAW 0201FF383501".ljust(84, b"\0")
    p = tmp_path / "DSCF0001.RAF"
    p.write_bytes(head + struct.pack(">II", 92, len(jpg)) + jpg)
    assert previews.embedded_jpeg(p) == (jpg, 1)


@pytest.mark.parametrize("data", [b"", b"II*\0" + b"\xff" * 4, b"not a raw file at all",
                                  b"II*\0\x08\0\0\0\xff\xff"])
def test_embedded_jpeg_never_raises(tmp_path, data):
    p = tmp_path / "x.NEF"
    p.write_bytes(data)
    assert previews.embedded_jpeg(p) is None
    assert previews.embedded_jpeg(tmp_path / "missing.NEF") is None


def test_raw_preview_is_rotated_by_the_raw_orientation(tmp_path):
    p = tmp_path / "DSC_0002.NEF"
    p.write_bytes(fake_nef(jpeg_bytes(600, 400), orientation=6))
    pv = previews.load_preview(p, 300)
    assert pv is not None and pv.source == "raw (embedded preview)"
    assert (pv.width, pv.height) == (400, 600)          # portrait after rotating
    assert pv.image.size[0] < pv.image.size[1] and max(pv.image.size) <= 300


def test_jpeg_preview_scaled_and_oriented(tmp_path):
    p = tmp_path / "DSC_0003.JPG"
    p.write_bytes(jpeg_bytes(1600, 1200, orientation=8))
    pv = previews.load_preview(p, 200)
    assert pv is not None and pv.source == "jpg"
    assert (pv.width, pv.height) == (1200, 1600)
    assert max(pv.image.size) <= 200 and pv.image.size[1] > pv.image.size[0]


def test_no_preview_for_video_or_garbage(tmp_path):
    (tmp_path / "DSC_0004.MOV").write_bytes(b"\0\0\0\x14ftypqt  " + b"\0" * 100)
    (tmp_path / "DSC_0005.JPG").write_bytes(b"\xff\xd8 broken")
    assert previews.load_preview(tmp_path / "DSC_0004.MOV", 200) is None
    assert previews.load_preview(tmp_path / "DSC_0005.JPG", 200) is None


def test_without_pillow_everything_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(previews, "_pil_checked", True)
    monkeypatch.setattr(previews, "_pil", None)
    p = tmp_path / "DSC_0006.JPG"
    p.write_bytes(jpeg_bytes(100, 80))
    assert previews.pillow() is None
    assert previews.load_preview(p, 100) is None


def _touch(p: Path, data: bytes = b"x", mtime: int = 1_700_000_000) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))


def test_list_library_groups_shots(tmp_path, fmt):
    ev = tmp_path / "2026.10.18 - oct dump"
    _touch(ev / "RAW" / "DSC_0002.NEF", mtime=1_700_000_200)
    _touch(ev / "JPG" / "DSC_0002.JPG", mtime=1_700_000_200)
    _touch(ev / "JPG" / "DSC_0002-v2.JPG", mtime=1_700_000_200)
    _touch(ev / "RAW" / "DSC_0001.NEF", mtime=1_700_000_100)
    _touch(ev / "JPG" / "DSC_0003.JPG", mtime=1_700_000_300)
    _touch(ev / "MOV" / "DSC_0004.MOV", mtime=1_700_000_400)
    _touch(ev / "RAW" / "DSC_0001.NEF.xmp", mtime=1_700_000_100)
    _touch(ev / "JPG" / ".DSC_0009.JPG.rji-abc123.part")
    _touch(ev / "notes.txt", mtime=1_700_000_500)
    shots = previews.list_library(ev, fmt)
    assert [s.name for s in shots] == ["DSC_0001", "DSC_0002", "DSC_0003", "DSC_0004", "notes"]
    by = {s.name: s for s in shots}
    assert by["DSC_0001"].kinds == ["raw", "sidecar"]
    assert by["DSC_0002"].kinds == ["raw", "jpg"] and len(by["DSC_0002"].files) == 3
    assert by["DSC_0004"].kinds == ["video"]
    assert [f.path.name for f in by["DSC_0002"].preview_order()] == [
        "DSC_0002.JPG", "DSC_0002-v2.JPG", "DSC_0002.NEF"]
    assert by["DSC_0001"].files[0].folder == "RAW"


def test_online_only_files_are_never_read_for_thumbnails(tmp_path, fmt, monkeypatch):
    from raw_jpg_import.gui import viewer

    ev = tmp_path / "ev"
    _touch(ev / "JPG" / "A.JPG", jpeg_bytes(80, 60))
    monkeypatch.setattr(previews, "is_online_only", lambda st: True)
    shots = previews.list_library(ev, fmt)
    assert shots[0].in_cloud and shots[0].files[0].online_only
    opened = []
    monkeypatch.setattr(previews, "load_preview", lambda p, s: opened.append(p))
    assert viewer._first_preview(shots[0].files, 100) == (None, None)
    assert opened == []


def test_summary_text_counts_pairs(tmp_path, fmt):
    from raw_jpg_import.gui.viewer import summary_text

    ev = tmp_path / "ev"
    for name in ("RAW/A.NEF", "JPG/A.JPG", "RAW/B.NEF", "JPG/C.JPG", "MOV/D.MOV"):
        _touch(ev / name)
    text = summary_text(previews.list_library(ev, fmt))
    assert text == ("4 shots · 1 RAW + JPG · 1 RAW without JPG · 1 JPG without RAW · 1 video")
