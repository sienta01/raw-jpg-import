"""exifread (dev-only) checks that the fake writers follow the TIFF/Exif spec."""

import io
from datetime import datetime

import pytest
from fakes.bmff import make_heif
from fakes.jpeg import make_jpeg
from fakes.raw import make_nef
from fakes.tiff import make_tiff

exifread = pytest.importorskip("exifread")

DTO = datetime(2026, 1, 23, 23, 2, 15)
DTO_TEXT = "2026:01:23 23:02:15"


def read_tags(data: bytes) -> dict:
    return exifread.process_file(io.BytesIO(data), details=False)


@pytest.mark.parametrize("bo", ["<", ">"])
@pytest.mark.parametrize("builder", ["tiff", "nef", "jpeg"])
def test_exifread_agrees(builder, bo):
    kw = {"bo": bo, "subsec": "34", "offset": "+07:00",
          "ifd0_datetime": datetime(2026, 2, 1, 10, 0, 0)}
    if builder == "tiff":
        data = make_tiff(DTO, **kw)
    elif builder == "nef":
        data = make_nef(DTO, size=8192, **kw)
    else:
        data = make_jpeg(DTO, size=8192, **kw)
    tags = read_tags(data)
    assert str(tags["EXIF DateTimeOriginal"]) == DTO_TEXT
    assert str(tags["EXIF SubSecTimeOriginal"]) == "34"
    assert str(tags["EXIF OffsetTimeOriginal"]) == "+07:00"
    assert str(tags["Image DateTime"]) == "2026:02:01 10:00:00"
    assert str(tags["Image Make"]) == "NIKON CORPORATION"
    assert str(tags["Image Model"]) == "NIKON Z f"


def test_exifread_reads_inline_ascii():
    tags = read_tags(make_tiff(DTO, make="AB", model="Zf", subsec="12"))
    assert str(tags["Image Make"]) == "AB"
    assert str(tags["Image Model"]) == "Zf"
    assert str(tags["EXIF SubSecTimeOriginal"]) == "12"


# ExifRead's HEIF reader only handles absolute iloc offsets, u16 iinf counts and (without
# the Exif prefix) little-endian TIFF, so only those layouts are cross-checked here.
@pytest.mark.parametrize("bo, exif_prefix", [("<", True), (">", True), ("<", False)])
def test_exifread_agrees_on_heif(bo, exif_prefix):
    data = make_heif(DTO, brand=b"heic", iloc_version=2, infe_version=2, bo=bo,
                     exif_prefix=exif_prefix, size=4000)
    tags = read_tags(data)
    assert str(tags["EXIF DateTimeOriginal"]) == DTO_TEXT
    assert str(tags["Image Make"]) == "NIKON CORPORATION"
