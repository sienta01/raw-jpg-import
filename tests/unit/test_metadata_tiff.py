import struct
from datetime import datetime

import pytest
from fakes.files import mtime_ns, write_file
from fakes.raw import make_arw, make_dng, make_nef, make_orf, make_rw2
from fakes.tiff import make_tiff

from raw_jpg_import.metadata import clear_cache, read_capture, read_capture_bytes
from raw_jpg_import.metadata._io import RandomAccess
from raw_jpg_import.metadata.tiff import parse_tiff

DTO = datetime(2026, 1, 23, 23, 2, 15)
CARD_MTIME = datetime(2026, 1, 23, 23, 2, 17)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def info(data: bytes):
    return parse_tiff(RandomAccess.from_bytes(data))


@pytest.mark.parametrize("bo", ["<", ">"])
def test_byte_orders(bo):
    i = info(make_tiff(DTO, bo=bo, subsec="34", offset="+07:00"))
    assert i.dto == DTO
    assert i.subsec_ms == 340
    assert i.offset == "+07:00"
    assert i.make == "NIKON CORPORATION"
    assert i.model == "NIKON Z f"


@pytest.mark.parametrize("magic", [42, 0x4F52, 0x5352, 0x55])
@pytest.mark.parametrize("bo", ["<", ">"])
def test_accepted_magics(magic, bo):
    cap = read_capture_bytes(make_tiff(DTO, bo=bo, magic=magic), "x.raw")
    assert (cap.local, cap.source, cap.exact) == (DTO, "exif", True)


def test_bigtiff_and_unknown_magic_rejected():
    assert info(make_tiff(DTO, magic=43)) is None
    assert info(make_tiff(DTO, magic=0x1234)) is None
    assert read_capture_bytes(make_tiff(DTO, magic=43), "x.nef", 0).source == "mtime"


def test_dto_beats_dtd_and_datetime():
    data = make_tiff(DTO, dtd=datetime(2026, 1, 23, 23, 2, 16),
                     ifd0_datetime=datetime(2026, 2, 1, 10, 0, 0))
    assert read_capture_bytes(data, "a.nef").local == DTO


def test_dtd_then_datetime_when_dto_missing():
    dtd = datetime(2026, 1, 23, 23, 2, 16)
    modified = datetime(2026, 2, 1, 10, 0, 0)
    assert read_capture_bytes(make_tiff(None, dtd=dtd, ifd0_datetime=modified),
                              "a.nef").local == dtd
    cap = read_capture_bytes(make_tiff(None, ifd0_datetime=modified, subsec="5"), "a.nef")
    assert cap.local == modified
    assert cap.subsec_ms is None        # sub-seconds belong to DateTimeOriginal only


@pytest.mark.parametrize("bad", ["0000:00:00 00:00:00", "    :  :     :  :  ",
                                 "2026:13:45 25:61:00", "garbage", ""])
def test_zero_or_invalid_date_falls_back_to_mtime(bad, tmp_path):
    path = write_file(tmp_path / "DSC_0001.NEF", make_nef(bad), CARD_MTIME)
    cap = read_capture(path)
    assert cap.source == "mtime"
    assert cap.exact is False
    assert cap.local == CARD_MTIME


def test_zero_dto_uses_datetime_instead():
    modified = datetime(2026, 1, 23, 23, 2, 15)
    cap = read_capture_bytes(make_tiff("0000:00:00 00:00:00", ifd0_datetime=modified), "a.nef")
    assert (cap.local, cap.source) == (modified, "exif")


@pytest.mark.parametrize("subsec, ms", [("34", 340), ("5", 500), ("123", 123),
                                        ("123456", 123), ("00", 0), ("ab", None)])
def test_subsec(subsec, ms):
    assert read_capture_bytes(make_tiff(DTO, subsec=subsec), "a.nef").subsec_ms == ms


@pytest.mark.parametrize("bo", ["<", ">"])
def test_inline_ascii_values(bo):
    # count <= 4 means the value sits inside the 12-byte entry itself
    i = info(make_tiff(DTO, bo=bo, make="AB", model="Zf", subsec="12"))
    assert (i.make, i.model, i.subsec_ms) == ("AB", "Zf", 120)


@pytest.mark.parametrize("bo", ["<", ">"])
def test_exif_tags_in_ifd0(bo):
    i = info(make_tiff(DTO, bo=bo, exif_in_ifd0=True, subsec="7", offset="-05:00"))
    assert (i.dto, i.subsec_ms, i.offset) == (DTO, 700, "-05:00")


def test_invalid_offset_ignored():
    assert info(make_tiff(DTO, offset="+7")).offset is None
    assert info(make_tiff(DTO, offset="+25:00")).offset is None


def test_tiff_at_base_offset():
    data = b"\x00" * 100 + make_tiff(DTO, bo=">")
    assert parse_tiff(RandomAccess.from_bytes(data), 100).dto == DTO


@pytest.mark.parametrize("builder, ext", [(make_nef, "NEF"), (make_arw, "ARW"),
                                          (make_dng, "DNG"), (make_orf, "ORF"),
                                          (make_rw2, "RW2")])
def test_raw_builders_round_trip(builder, ext, tmp_path):
    path = write_file(tmp_path / f"X.{ext}", builder(DTO, subsec="50"), CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source, cap.exact, cap.subsec_ms) == (DTO, "exif", True, 500)


def test_raw_padding_is_seeded_by_name():
    a = make_nef(DTO, name="DSC_0001.NEF")
    b = make_nef(DTO, name="DSC_0002.NEF")
    assert a == make_nef(DTO, name="DSC_0001.NEF")
    assert a != b and len(a) != len(b)
    assert 64 * 1024 <= len(a) < 72 * 1024


def test_big_raw_not_multiple_of_chunk(tmp_path):
    size = 3 * 1024 * 1024 + 512 * 1024
    data = make_nef(DTO, size=size)
    assert len(data) == size and size % (4 * 1024 * 1024)
    path = write_file(tmp_path / "BIG.NEF", data, CARD_MTIME)
    assert read_capture(path).local == DTO


@pytest.mark.parametrize("year, suspicious", [(1980, True), (1995, False), (2026, False)])
def test_suspicious_years(year, suspicious):
    cap = read_capture_bytes(make_tiff(DTO.replace(year=year)), "a.nef")
    assert cap.suspicious is suspicious


def test_future_date_is_suspicious():
    future = datetime.now().replace(microsecond=0)
    future = future.replace(year=future.year + 2)
    assert read_capture_bytes(make_tiff(future), "a.nef").suspicious is True


def test_ifd_loop_and_huge_counts_do_not_hang():
    # IFD0 whose ExifIFD pointer points back at IFD0 itself
    entry = struct.pack("<HHII", 0x8769, 4, 1, 8)
    loop = b"II*\x00" + struct.pack("<IH", 8, 1) + entry + b"\0\0\0\0"
    assert info(loop) is not None and info(loop).dto is None
    huge = b"II*\x00" + struct.pack("<IH", 8, 60000) + b"\0" * 64
    assert info(huge).dto is None


def test_mtime_of_card_file_is_local_wall_clock(tmp_path):
    path = write_file(tmp_path / "x.nef", b"not a tiff at all", CARD_MTIME)
    cap = read_capture(path)
    assert cap.local == CARD_MTIME
    assert cap.source == "mtime"
    assert mtime_ns(CARD_MTIME) == path.stat().st_mtime_ns
