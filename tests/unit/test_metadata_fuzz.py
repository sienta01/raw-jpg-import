"""Seeded truncations and bit flips: parsers must never raise and never invent garbage types."""

import random
from datetime import datetime

import pytest
from fakes.bmff import make_heif, make_mov, make_mp4, video_mtime
from fakes.files import mtime_ns
from fakes.jpeg import make_jpeg
from fakes.raw import make_cr3, make_nef, make_raf

from raw_jpg_import.metadata import (
    Capture,
    detect_kind,
    parse_capture,
    read_capture_bytes,
)
from raw_jpg_import.metadata._io import ParseError, RandomAccess
from raw_jpg_import.models import EXACT_SOURCES

WHEN = datetime(2026, 1, 23, 23, 2, 15)
SMALL = 3000
CASES = 200
SOURCES = {"exif", "heif-exif", "cr3", "raf", "nctg", "qt-keys", "mvhd+mtime", "mvhd-pc-tz",
           "mtime"}

BUILDERS = {
    "nef-le": ("a.nef", lambda: make_nef(WHEN, size=SMALL, subsec="12", offset="+07:00")),
    "nef-be": ("a.nef", lambda: make_nef(WHEN, size=SMALL, bo=">")),
    "jpeg": ("a.jpg", lambda: make_jpeg(WHEN, size=SMALL, fill_bytes=2)),
    "heif-v1-idat": ("a.hif", lambda: make_heif(WHEN, size=SMALL, iloc_version=1,
                                                construction=1)),
    "heif-v2-mdat": ("a.hif", lambda: make_heif(WHEN, size=SMALL, iloc_version=2)),
    "mov-nctg": ("a.mov", lambda: make_mov(WHEN, 420, 12.0, size=SMALL)),
    "mov-keys": ("a.mov", lambda: make_mov(WHEN, 420, 12.0, size=SMALL, nctg=False,
                                           keys=True, day=True, moov_at_end=False)),
    "mov-mvhd": ("a.mov", lambda: make_mov(WHEN, -300, 12.0, size=SMALL, nctg=False,
                                           mvhd_version=1, largesize=True)),
    "mp4": ("a.mp4", lambda: make_mp4(WHEN, 60, 12.0, size=SMALL)),
    "raf": ("a.raf", lambda: make_raf(WHEN, size=SMALL + 9000)),
    "cr3": ("a.cr3", lambda: make_cr3(WHEN, size=SMALL)),
}

MTIME_NS = mtime_ns(video_mtime(WHEN, 12.0))


def mutate(data: bytes, rng: random.Random) -> bytes:
    kind = rng.randrange(3)
    if kind == 0 or len(data) < 8:
        return data[:rng.randrange(max(1, len(data)))]
    buf = bytearray(data)
    if kind == 1:
        for _ in range(rng.randint(1, 8)):
            pos = rng.randrange(len(buf))
            buf[pos] ^= 1 << rng.randrange(8)
    else:
        pos = rng.randrange(len(buf) - 4)
        buf[pos:pos + 4] = rng.choice([b"\x00" * 4, b"\xff" * 4, rng.randbytes(4),
                                       b"\x00\x00\x00\x01"])
    return bytes(buf)


def assert_valid(cap: Capture) -> None:
    assert isinstance(cap, Capture)
    assert isinstance(cap.local, datetime) and cap.local.tzinfo is None
    assert cap.source in SOURCES
    assert cap.exact == (cap.source in EXACT_SOURCES)
    assert cap.subsec_ms is None or 0 <= cap.subsec_ms <= 999
    assert isinstance(cap.suspicious, bool)


@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_mutations_never_raise(builder):
    name, make = BUILDERS[builder]
    original = make()
    assert read_capture_bytes(original, name, MTIME_NS).source != "mtime"
    rng = random.Random(f"fuzz:{builder}")
    for _ in range(CASES):
        data = mutate(original, rng)
        assert_valid(read_capture_bytes(data, name, MTIME_NS))
        # The parsers themselves may only fail with ParseError (anything else is a bug).
        kind = detect_kind(data[:16], name.rpartition(".")[2])
        if kind is not None:
            try:
                cap = parse_capture(RandomAccess.from_bytes(data), kind, MTIME_NS)
            except ParseError:
                continue
            if cap is not None:
                assert_valid(cap)


@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_every_truncation_of_the_header(builder):
    name, make = BUILDERS[builder]
    original = make()
    for n in range(0, min(len(original), 400), 7):
        assert_valid(read_capture_bytes(original[:n], name, MTIME_NS))


def test_random_bytes_with_every_extension():
    rng = random.Random("noise")
    for ext in ("nef", "jpg", "hif", "cr3", "raf", "mov", "mp4", "xyz", ""):
        for _ in range(20):
            data = rng.randbytes(rng.randrange(1, 600))
            assert_valid(read_capture_bytes(data, f"a.{ext}", MTIME_NS))
