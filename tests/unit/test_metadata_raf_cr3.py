from datetime import datetime

import pytest
from fakes.files import write_file
from fakes.raw import RAF_MAGIC, make_cr3, make_raf

from raw_jpg_import.metadata import clear_cache, read_capture, read_capture_bytes
from raw_jpg_import.metadata._io import RandomAccess
from raw_jpg_import.metadata.bmff import cr3_exif
from raw_jpg_import.metadata.raf import parse_raf

DTO = datetime(2026, 1, 23, 23, 2, 15)
CARD_MTIME = datetime(2026, 1, 24, 9, 0, 0)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.mark.parametrize("bo", ["<", ">"])
def test_raf_round_trip(bo, tmp_path):
    data = make_raf(DTO, bo=bo, subsec="10")
    assert data.startswith(RAF_MAGIC)
    path = write_file(tmp_path / "DSCF0001.RAF", data, CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source, cap.exact, cap.subsec_ms) == (DTO, "raf", True, 100)
    assert parse_raf(RandomAccess.from_bytes(data)).make == "FUJIFILM"


def test_raf_bad_jpeg_pointer_falls_back():
    data = bytearray(make_raf(DTO))
    data[84:88] = (len(data) + 10).to_bytes(4, "big")
    assert read_capture_bytes(bytes(data), "a.raf", 0).source == "mtime"


def test_raf_wrong_magic():
    assert parse_raf(RandomAccess.from_bytes(b"FUJIFILM" + b"\x00" * 200)) is None


@pytest.mark.parametrize("bo", ["<", ">"])
def test_cr3_round_trip(bo, tmp_path):
    path = write_file(tmp_path / "IMG_0001.CR3", make_cr3(DTO, bo=bo), CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source, cap.exact, cap.subsec_ms) == (DTO, "cr3", True, 0)


def test_cr3_cmt2_beats_cmt1_and_merges_make():
    data = make_cr3(DTO, ifd0_datetime=datetime(2026, 2, 2, 2, 2, 2))
    info = cr3_exif(RandomAccess.from_bytes(data))
    assert info.dto == DTO
    assert info.make == "Canon" and info.model == "Canon EOS R6m2"
    assert read_capture_bytes(data, "a.cr3").local == DTO


def test_cr3_dispatch_by_brand():
    assert read_capture_bytes(make_cr3(DTO), "a.bin").source == "cr3"


def test_cr3_without_canon_uuid():
    from fakes.bmff import box, ftyp
    data = ftyp(b"crx ") + box(b"moov", box(b"free")) + box(b"mdat", b"\x00" * 64)
    assert cr3_exif(RandomAccess.from_bytes(data)) is None
