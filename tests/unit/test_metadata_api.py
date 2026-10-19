from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
from fakes.bmff import make_heif, make_mov, video_mtime
from fakes.files import mtime_ns, write_file
from fakes.jpeg import make_jpeg
from fakes.raw import make_nef
from fakes.tiff import make_tiff

from raw_jpg_import import metadata
from raw_jpg_import.errors import Cancelled
from raw_jpg_import.events import CancelToken, Progress
from raw_jpg_import.formats import split_ext
from raw_jpg_import.metadata import (
    capture_from_mtime,
    clear_cache,
    detect_kind,
    read_capture,
    read_many,
)
from raw_jpg_import.metadata._io import MAX_PAGES, PAGE_SIZE, Cursor, ParseError, RandomAccess
from raw_jpg_import.models import Source, SourceFile

A = datetime(2026, 1, 23, 23, 2, 15)
B = datetime(2025, 6, 1, 8, 0, 0)
CARD_MTIME = datetime(2026, 1, 23, 23, 2, 18)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def source_file(path: Path, fmt) -> SourceFile:
    st = path.stat()
    stem, ext = split_ext(path.name)
    return SourceFile(source=Source(root=path.parent, label="CARD"), path=path,
                      rel=PurePosixPath(path.name), name=path.name, stem=stem, ext=ext,
                      size=st.st_size, mtime_ns=st.st_mtime_ns, readonly=False,
                      category=fmt.classify(path.name))


# --- read_capture -------------------------------------------------------------------------


def test_stat_defaults_and_cache(tmp_path):
    path = write_file(tmp_path / "DSC_0001.NEF", make_nef(A, size=5000), CARD_MTIME)
    assert read_capture(path).local == A
    # Same size and mtime: the cached answer is returned without reading the file.
    write_file(path, make_nef(B, size=5000), CARD_MTIME)
    assert read_capture(path).local == A
    st = path.stat()
    assert read_capture(path, size=st.st_size, mtime_ns=st.st_mtime_ns).local == A
    clear_cache()
    assert read_capture(path).local == B


def test_cache_key_includes_size_and_mtime(tmp_path):
    path = write_file(tmp_path / "a.nef", make_nef(A, size=5000), CARD_MTIME)
    assert read_capture(path).local == A
    write_file(path, make_nef(B, size=5000), datetime(2026, 1, 23, 23, 2, 20))
    assert read_capture(path).local == B


def test_missing_file_never_raises_and_is_not_cached(tmp_path):
    path = tmp_path / "gone.nef"
    ns = mtime_ns(CARD_MTIME)
    cap = read_capture(path, size=5000, mtime_ns=ns)
    assert (cap.local, cap.source) == (CARD_MTIME, "mtime")
    write_file(path, make_nef(A, size=5000), CARD_MTIME)
    assert read_capture(path, size=5000, mtime_ns=ns).local == A


@pytest.mark.parametrize("bad", ["missing", "dir", "nul"])
def test_never_raises(bad, tmp_path):
    target = {"missing": tmp_path / "nope.jpg", "dir": tmp_path, "nul": "bad\x00name.jpg"}[bad]
    cap = read_capture(target)
    assert cap.source == "mtime"


def test_parser_bug_falls_back(tmp_path, monkeypatch):
    def boom(ra):
        raise RuntimeError("bug")
    monkeypatch.setitem(metadata._EXIF_PARSERS, "tiff", (boom, "exif"))
    path = write_file(tmp_path / "a.nef", make_nef(A), CARD_MTIME)
    cap = read_capture(path)
    assert (cap.local, cap.source) == (CARD_MTIME, "mtime")


def test_capture_from_mtime():
    cap = capture_from_mtime(mtime_ns(CARD_MTIME))
    assert (cap.local, cap.source, cap.exact, cap.suspicious) == (CARD_MTIME, "mtime", False,
                                                                 False)
    assert capture_from_mtime(0).suspicious is True
    assert capture_from_mtime(-10**18).source == "mtime"


# --- dispatch -----------------------------------------------------------------------------


@pytest.mark.parametrize("name, data, source", [
    ("DSC_0001.NEF", make_jpeg(A, size=4000), "exif"),        # JPEG content, raw name
    ("DSC_0001.JPG", make_tiff(A), "exif"),
    ("DSC_0001.XYZ", make_tiff(A), "exif"),
    ("DSC_0001.JPG", make_heif(A, size=4000), "heif-exif"),
    ("noext", make_jpeg(A, size=4000), "exif"),
    ("DSC_0001.NEF", b"plain text, not a raw", "mtime"),
    ("notes.txt", b"hello", "mtime"),
])
def test_magic_beats_extension(name, data, source, tmp_path):
    path = write_file(tmp_path / name, data, CARD_MTIME)
    assert read_capture(path).source == source


def test_detect_kind():
    assert detect_kind(b"\x00" * 16, "nef") == "tiff"
    assert detect_kind(b"\x00" * 16, "mov") == "video"
    assert detect_kind(b"\x00" * 16, "hif") == "heif"
    assert detect_kind(b"\x00" * 16, "xyz") is None
    assert detect_kind(b"", "") is None
    assert detect_kind(b"\x00\x00\x00\x18ftypheix", "mov") == "video"
    assert detect_kind(b"\x00\x00\x00\x18ftypqt  ", "hif") == "heif"
    assert detect_kind(b"\x00\x00\x00\x18ftypcrx ", "jpg") == "cr3"
    assert detect_kind(b"\x00\x00\x00\x08wide", "bin") == "video"


# --- read_many ----------------------------------------------------------------------------


def make_files(tmp_path, fmt, n=6):
    files = []
    for i in range(n):
        p = write_file(tmp_path / f"DSC_{i:04d}.NEF", make_nef(A, name=f"N{i}", size=3000),
                       CARD_MTIME)
        files.append(source_file(p, fmt))
    mov = write_file(tmp_path / "DSC_9000.MOV", make_mov(A, 420, 10.0, size=4000),
                     video_mtime(A, 10.0))
    junk = write_file(tmp_path / "DSC_9001.JPG", b"broken", CARD_MTIME)
    return [*files, source_file(mov, fmt), source_file(junk, fmt)]


def test_read_many(tmp_path, fmt):
    files = make_files(tmp_path, fmt)
    events = []
    caps = read_many(files, workers=3, emit=events.append)
    assert set(caps) == {f.path for f in files}
    assert caps[files[0].path].local == A
    assert caps[files[-2].path].source == "nctg"
    assert caps[files[-1].path].source == "mtime"
    progress = [e for e in events if isinstance(e, Progress)]
    assert progress and all(e.phase == "metadata" for e in progress)
    assert progress[0].files_done == 0
    assert progress[-1].files_done == progress[-1].files_total == len(files)
    # A second pass is served from the cache.
    assert read_many(files) == caps


def test_read_many_empty():
    events = []
    assert read_many([], emit=events.append) == {}
    assert events[-1].files_total == 0


def test_read_many_cancelled_before_start(tmp_path, fmt):
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        read_many(make_files(tmp_path, fmt), cancel=token)


def test_read_many_cancelled_while_running(tmp_path, fmt):
    token = CancelToken()
    with pytest.raises(Cancelled):
        read_many(make_files(tmp_path, fmt), cancel=token, emit=lambda e: token.cancel())


# --- _io ----------------------------------------------------------------------------------


DATA = bytes(range(256)) * 1100      # a bit over 4 pages


def test_reads_across_pages_and_lru():
    ra = RandomAccess.from_bytes(DATA)
    assert ra.read_at(PAGE_SIZE - 3, 10) == DATA[PAGE_SIZE - 3:PAGE_SIZE + 7]
    assert ra.read_at(0, len(DATA)) == DATA          # large read bypasses the cache
    big = RandomAccess.from_bytes(bytes(PAGE_SIZE * (MAX_PAGES + 5)))
    for i in range(MAX_PAGES + 5):
        big.read_at(i * PAGE_SIZE, 1)
    assert len(big._pager._pages) == MAX_PAGES


def test_bounds_checked():
    ra = RandomAccess.from_bytes(DATA)
    for off, n in [(-1, 1), (len(DATA) - 1, 2), (0, -1), (len(DATA), 1)]:
        with pytest.raises(ParseError):
            ra.read_at(off, n)
    assert ra.read_upto(len(DATA) - 2, 10) == DATA[-2:]
    assert ra.read_upto(len(DATA) + 5, 10) == b""


def test_window():
    ra = RandomAccess.from_bytes(DATA)
    w = ra.window(10, 100)
    assert w.size == 100 and w.read_at(0, 5) == DATA[10:15]
    assert w.u16(0) == int.from_bytes(DATA[10:12], "big")
    assert w.u32(0, "<") == int.from_bytes(DATA[10:14], "little")
    with pytest.raises(ParseError):
        w.read_at(99, 2)
    with pytest.raises(ParseError):
        ra.window(len(DATA) - 5, 10)
    assert w.window(5, 10).read_at(0, 3) == DATA[15:18]


def test_file_backed_reader(tmp_path):
    path = write_file(tmp_path / "blob.bin", DATA)
    with RandomAccess.open(path) as ra:
        assert ra.size == len(DATA)
        assert ra.read_at(PAGE_SIZE * 2 - 1, 3) == DATA[PAGE_SIZE * 2 - 1:PAGE_SIZE * 2 + 2]


def test_cursor():
    cur = Cursor(b"\x00\x01\x00\x00\x00\x02rest")
    assert (cur.u16(), cur.u32(), cur.remaining) == (1, 2, 4)
    assert cur.uint(0) == 0
    with pytest.raises(ParseError):
        cur.uint(3)
    with pytest.raises(ParseError):
        cur.take(5)
