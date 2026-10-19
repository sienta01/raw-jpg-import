from datetime import UTC, datetime, timedelta

import pytest
from fakes.bmff import box, ftyp, make_mov, make_mp4, video_mtime
from fakes.files import write_file

from raw_jpg_import.metadata import clear_cache, read_capture, read_capture_bytes
from raw_jpg_import.metadata.quicktime import _nctg_capture

START = datetime(2026, 1, 23, 23, 2, 15)      # camera wall clock when recording started
DURATION = 12.0


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def write_clip(tmp_path, data, *, name="DSC_0001.MOV", mtime=None):
    return write_file(tmp_path / name, data, mtime or video_mtime(START, DURATION))


def pc_local(utc_naive: datetime) -> datetime:
    return utc_naive.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)


@pytest.mark.parametrize("tz", [420, -300, 0])
def test_nctg_is_local_time(tz, tmp_path):
    cap = read_capture(write_clip(tmp_path, make_mov(START, tz, DURATION)))
    assert (cap.local, cap.source, cap.exact) == (START, "nctg", True)


def test_nctg_little_endian_records():
    cap = read_capture_bytes(make_mov(START, 420, DURATION, nctg_bo="<"), "a.mov")
    assert (cap.local, cap.source) == (START, "nctg")


def test_nctg_dto_beats_create_date():
    data = make_mov(START, 420, DURATION, nctg_create=START + timedelta(seconds=5))
    assert read_capture_bytes(data, "a.mov").local == START


def test_nctg_create_date_alone():
    data = make_mov(START, 420, DURATION, nctg_tag=0x11)
    assert read_capture_bytes(data, "a.mov").local == START


def test_nctg_regex_fallback_for_unknown_tag():
    data = make_mov(START, 420, DURATION, nctg_tag=0x1002)
    cap = read_capture_bytes(data, "a.mov")
    assert (cap.local, cap.source) == (START, "nctg")


@pytest.mark.parametrize("bo", [">", "<"])
def test_nctg_timezone_int16(bo):
    data = make_mov(START, -330, DURATION, nctg_timezone=True, nctg_bo=bo)
    assert read_capture_bytes(data, "a.mov").offset == "-05:30"


def test_nctg_gps_tag_with_same_low_bits_is_ignored():
    from fakes.bmff import nctg_record
    recs = (nctg_record(0x1200012, 2, 7, b"WGS-84\x00")
            + nctg_record(0x12, 2, 20, b"2026:01:23 23:02:15\x00"))
    assert _nctg_capture(recs).local == START


@pytest.mark.parametrize("meta_fullbox", [False, True])
def test_keys_creationdate_keeps_wall_clock(meta_fullbox, tmp_path):
    data = make_mov(START, 420, DURATION, nctg=False, keys=True, meta_fullbox=meta_fullbox)
    cap = read_capture(write_clip(tmp_path, data, mtime=datetime(2030, 1, 1)))
    assert (cap.local, cap.source, cap.exact, cap.offset) == (START, "qt-keys", True, "+07:00")


def test_day_box(tmp_path):
    data = make_mov(START, -300, DURATION, nctg=False, day=True)
    cap = read_capture(write_clip(tmp_path, data))
    assert (cap.local, cap.source, cap.offset) == (START, "qt-keys", "-05:00")


def test_nctg_beats_keys():
    data = make_mov(START, 420, DURATION, keys=True, nctg_tag=0x12)
    assert read_capture_bytes(data, "a.mov").source == "nctg"


@pytest.mark.parametrize("tz", [420, -300, 345, 0, -570])
@pytest.mark.parametrize("version", [0, 1])
def test_mvhd_offset_inferred_from_mtime(tz, version, tmp_path):
    data = make_mov(START, tz, DURATION, nctg=False, mvhd_version=version)
    cap = read_capture(write_clip(tmp_path, data))
    assert (cap.local, cap.source, cap.exact) == (START, "mvhd+mtime", False)


def test_mvhd_tolerates_late_close_and_fat_rounding(tmp_path):
    late = video_mtime(START, DURATION) + timedelta(seconds=95)
    data = make_mov(START, 420, DURATION, nctg=False)
    assert read_capture(write_clip(tmp_path, data, mtime=late)).local == START


def test_mvhd_with_unrelated_mtime_uses_pc_time_zone(tmp_path):
    # 37 minutes off any quarter hour: the mtime does not explain the mvhd time
    odd = video_mtime(START, DURATION) + timedelta(minutes=37)
    data = make_mov(START, 420, DURATION, nctg=False)
    cap = read_capture(write_clip(tmp_path, data, mtime=odd))
    assert cap.source == "mvhd-pc-tz"
    assert cap.local == pc_local(START - timedelta(minutes=420))


def test_mvhd_without_mtime_uses_pc_time_zone():
    data = make_mov(START, 420, DURATION, nctg=False)
    cap = read_capture_bytes(data, "a.mov", 0)
    assert cap.source == "mvhd-pc-tz"


@pytest.mark.parametrize("moov_at_end", [True, False])
@pytest.mark.parametrize("largesize", [False, True])
def test_moov_position_and_largesize(moov_at_end, largesize, tmp_path):
    data = make_mov(START, 420, DURATION, moov_at_end=moov_at_end, largesize=largesize)
    assert read_capture(write_clip(tmp_path, data)).source == "nctg"
    data = make_mov(START, 420, DURATION, nctg=False, moov_at_end=moov_at_end,
                    largesize=largesize)
    clear_cache()
    assert read_capture(write_clip(tmp_path, data)).local == START


def test_mdat_size_zero_to_end_of_file(tmp_path):
    data = make_mov(START, 420, DURATION, moov_at_end=False, mdat_size_zero=True)
    assert read_capture(write_clip(tmp_path, data)).source == "nctg"


def test_creation_time_zero_uses_mtime_minus_duration(tmp_path):
    data = make_mov(START, 420, DURATION, nctg=False, creation_time_zero=True)
    cap = read_capture(write_clip(tmp_path, data))
    assert (cap.local, cap.source, cap.exact) == (START, "mtime", False)


def test_mp4_mvhd(tmp_path):
    data = make_mp4(START, -300, DURATION)
    assert data[8:12] == b"mp42"
    cap = read_capture(write_clip(tmp_path, data, name="C0001.MP4"))
    assert (cap.local, cap.source) == (START, "mvhd+mtime")


def test_mp4_keys(tmp_path):
    cap = read_capture(write_clip(tmp_path, make_mp4(START, 60, DURATION, keys=True),
                                  name="C0001.MP4"))
    assert (cap.local, cap.source, cap.offset) == (START, "qt-keys", "+01:00")


def test_no_moov_falls_back_to_mtime(tmp_path):
    data = ftyp(b"qt  ") + box(b"mdat", b"\x00" * 1000)
    mtime = datetime(2026, 1, 23, 23, 5, 0)
    cap = read_capture(write_clip(tmp_path, data, mtime=mtime))
    assert (cap.local, cap.source) == (mtime, "mtime")


def test_big_clip(tmp_path):
    size = 3 * 1024 * 1024 + 512 * 1024
    data = make_mov(START, 420, DURATION, nctg=False, size=size)
    assert len(data) == size
    assert read_capture(write_clip(tmp_path, data)).source == "mvhd+mtime"


def test_damaged_keys_do_not_hide_mvhd(tmp_path):
    data = bytearray(make_mov(START, 420, DURATION, nctg=False, keys=True))
    first_entry = data.index(b"keys") + 12
    data[first_entry:first_entry + 4] = (3).to_bytes(4, "big")     # entry size < 8
    cap = read_capture(write_clip(tmp_path, bytes(data)))
    assert (cap.local, cap.source) == (START, "mvhd+mtime")


def test_nctg_without_date_falls_through_to_mvhd(tmp_path):
    data = make_mov(START, -300, DURATION).replace(b"2026:01:23", b"XXXX:XX:XX")
    cap = read_capture(write_clip(tmp_path, data))
    assert (cap.local, cap.source) == (START, "mvhd+mtime")
