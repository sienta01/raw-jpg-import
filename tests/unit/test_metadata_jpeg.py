from datetime import datetime

import pytest
from fakes.files import write_file
from fakes.jpeg import make_jpeg, segment
from fakes.tiff import make_tiff

from raw_jpg_import.metadata import clear_cache, read_capture, read_capture_bytes
from raw_jpg_import.metadata._io import RandomAccess
from raw_jpg_import.metadata.jpeg import parse_jpeg

DTO = datetime(2026, 1, 23, 23, 2, 15)
CARD_MTIME = datetime(2026, 1, 23, 23, 2, 18)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.mark.parametrize("bo", ["<", ">"])
def test_app0_before_app1(bo, tmp_path):
    data = make_jpeg(DTO, bo=bo, subsec="20", offset="+01:00")
    assert data[2:4] == b"\xff\xe0"
    path = write_file(tmp_path / "DSC_0001.JPG", data, CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source, cap.exact) == (DTO, "exif", True)
    assert (cap.subsec_ms, cap.offset) == (200, "+01:00")


def test_app1_first():
    data = make_jpeg(DTO, app0=False)
    assert data[2:4] == b"\xff\xe1"
    assert read_capture_bytes(data, "a.jpg").local == DTO


@pytest.mark.parametrize("fill", [1, 5])
def test_fill_bytes_before_marker(fill):
    data = make_jpeg(DTO, fill_bytes=fill)
    assert b"\xff" * (fill + 1) + b"\xe1" in data
    assert read_capture_bytes(data, "a.jpg").local == DTO


def test_no_app1_falls_back_to_mtime(tmp_path):
    path = write_file(tmp_path / "a.jpg", make_jpeg(DTO, exif=False), CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source, cap.exact) == (CARD_MTIME, "mtime", False)


def test_non_exif_app1_skipped():
    xmp = segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta/>")
    data = make_jpeg(DTO)
    data = data[:2] + xmp + data[2:]
    assert read_capture_bytes(data, "a.jpg").local == DTO


def test_exif_after_sos_is_not_found():
    data = make_jpeg(None, exif=False, body_size=32)
    late = segment(0xE1, b"Exif\x00\x00" + make_tiff(DTO))
    data = data[:-2] + late + data[-2:]
    assert parse_jpeg(RandomAccess.from_bytes(data)) is None


def test_body_size_and_default_size():
    small = make_jpeg(DTO, body_size=10)
    assert len(small) < 2048
    assert 64 * 1024 <= len(make_jpeg(DTO)) < 72 * 1024
    assert len(make_jpeg(DTO, size=100_000)) == 100_000


def test_truncated_inside_exif():
    data = make_jpeg(DTO)
    cut = data[:data.index(b"Exif") + 20]
    assert read_capture_bytes(cut, "a.jpg", 0).source == "mtime"


def test_not_a_jpeg():
    assert parse_jpeg(RandomAccess.from_bytes(b"\x00\x01\x02")) is None
    assert parse_jpeg(RandomAccess.from_bytes(b"\xff\xd8\x00\x00\x00")) is None
