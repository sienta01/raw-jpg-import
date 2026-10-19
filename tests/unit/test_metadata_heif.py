from datetime import datetime

import pytest
from fakes.bmff import box, ftyp, full_box, make_heif
from fakes.files import write_file

from raw_jpg_import.metadata import clear_cache, read_capture, read_capture_bytes
from raw_jpg_import.metadata._io import RandomAccess
from raw_jpg_import.metadata.bmff import heif_exif, iter_boxes

DTO = datetime(2026, 1, 23, 23, 2, 15)
CARD_MTIME = datetime(2026, 1, 23, 23, 2, 19)

LAYOUTS = [(0, 0), (1, 0), (1, 1), (2, 0), (2, 1)]   # (iloc version, construction method)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.mark.parametrize("iloc_version, construction", LAYOUTS)
@pytest.mark.parametrize("exif_prefix", [True, False])
def test_iloc_layouts(iloc_version, construction, exif_prefix, tmp_path):
    data = make_heif(DTO, iloc_version=iloc_version, construction=construction,
                     exif_prefix=exif_prefix, subsec="45", offset="+07:00")
    path = write_file(tmp_path / "DSC_0001.HIF", data, CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source, cap.exact) == (DTO, "heif-exif", True)
    assert (cap.subsec_ms, cap.offset) == (450, "+07:00")


def test_iloc_v0_has_no_construction_method():
    with pytest.raises(ValueError):
        make_heif(DTO, iloc_version=0, construction=1)


@pytest.mark.parametrize("brand", [b"heix", b"heic", b"mif1", b"hevx", b"zzzz"])
def test_brand_never_rejected(brand):
    cap = read_capture_bytes(make_heif(DTO, brand=brand), "DSC_0001.HIF")
    assert (cap.local, cap.source) == (DTO, "heif-exif")


@pytest.mark.parametrize("ext", ["HIF", "heif", "HEIC"])
def test_extensions(ext):
    assert read_capture_bytes(make_heif(DTO), f"x.{ext}").source == "heif-exif"


def test_unknown_extension_dispatches_on_heif_brand():
    assert read_capture_bytes(make_heif(DTO, brand=b"heic"), "x.bin").source == "heif-exif"


@pytest.mark.parametrize("exif_prefix", [True, False])
@pytest.mark.parametrize("bo", ["<", ">"])
def test_missing_offset_field_found_by_search(exif_prefix, bo):
    data = make_heif(DTO, exif_prefix=exif_prefix, offset_field=False, bo=bo)
    assert read_capture_bytes(data, "a.hif").local == DTO


def test_no_exif_item_falls_back_to_mtime(tmp_path):
    path = write_file(tmp_path / "a.hif", make_heif(DTO, with_exif=False), CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source) == (CARD_MTIME, "mtime")


def test_infe_versions_independent_of_iloc():
    for infe_version in (2, 3):
        data = make_heif(DTO, iloc_version=1, infe_version=infe_version)
        assert heif_exif(RandomAccess.from_bytes(data)).dto == DTO


def test_no_meta_box():
    data = ftyp(b"heic", (b"mif1",)) + box(b"mdat", b"\x00" * 100)
    assert heif_exif(RandomAccess.from_bytes(data)) is None
    assert read_capture_bytes(data, "a.heic", 0).source == "mtime"


def test_meta_without_iloc():
    data = ftyp(b"heic") + full_box(b"meta", 0, 0, box(b"hdlr", b"\x00" * 24))
    assert heif_exif(RandomAccess.from_bytes(data)) is None


def test_box_iterator_largesize_and_size_zero():
    data = box(b"free", b"ab") + box(b"mdat", b"x" * 40, largesize=True) + box(
        b"skip", b"tail", size_zero=True)
    boxes = list(iter_boxes(RandomAccess.from_bytes(data)))
    assert [b.type for b in boxes] == [b"free", b"mdat", b"skip"]
    assert boxes[1].header == 16 and boxes[1].payload_size == 40
    assert boxes[2].end == len(data)


def test_box_iterator_stops_at_bad_size():
    data = box(b"free", b"ab") + b"\x00\x00\x00\x04abcd" + box(b"free")
    assert [b.type for b in iter_boxes(RandomAccess.from_bytes(data))] == [b"free"]
    over = b"\x00\x00\x10\x00free" + b"\x00" * 8
    assert list(iter_boxes(RandomAccess.from_bytes(over))) == []
