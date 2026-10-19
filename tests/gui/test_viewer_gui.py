"""The photo viewer window, driven on a real Tk root (marker gui)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from test_gui_smoke import collect_on_the_tk_thread, new_tk  # noqa: F401  (fixture reused)

pytestmark = pytest.mark.gui
PIL = pytest.importorskip("PIL.Image")
tk = pytest.importorskip("tkinter")

from fakes.previews import fake_nef, jpeg_bytes  # noqa: E402  (needs Pillow)


def _jpeg(w: int, h: int, color) -> bytes:
    return jpeg_bytes(w, h, color)


def _put(p: Path, data: bytes, mtime: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))


@pytest.fixture
def event(tmp_path):
    ev = tmp_path / "2026.10.18 - oct dump"
    for n in range(1, 13):
        _put(ev / "JPG" / f"DSC_{n:04d}.JPG", _jpeg(900, 600, (20 * n, 90, 160)),
             1_700_000_000 + n)
        if n % 3:
            _put(ev / "RAW" / f"DSC_{n:04d}.NEF", fake_nef(_jpeg(1200, 800, (n, n, n))),
                 1_700_000_000 + n)
    _put(ev / "RAW" / "DSC_0013.NEF", fake_nef(_jpeg(1200, 800, (1, 2, 3)), orientation=6),
         1_700_000_013)
    _put(ev / "MOV" / "DSC_0014.MOV", b"\0" * 64, 1_700_000_014)
    return ev


@pytest.fixture
def root():
    import gc

    r = new_tk()
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass
    gc.collect()


def pump(root, until, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if until():
            return True
        time.sleep(0.02)
    return False


def test_viewer_grid_filters_and_large_view(root, event, fmt):
    from raw_jpg_import.gui.viewer import PhotoViewer

    closed = []
    v = PhotoViewer(root, [event], fmt, on_close=lambda: closed.append(True))
    v.withdraw()
    assert pump(root, lambda: len(v.shots) == 14)
    assert v.summary.cget("text") == ("14 shots · 8 RAW + JPG · 1 RAW without JPG · "
                                      "4 JPG without RAW · 1 video")
    v.geometry("1000x700")
    root.update()
    v.layout()
    # every visible photo tile gets a thumbnail; the video keeps its placeholder
    assert pump(root, lambda: len(v.thumbs) >= 13)
    assert "dsc_0014" not in v.thumbs

    v.filter_var.set("RAW without JPG")
    v.apply_filter()
    assert [s.name for s in v.shown] == ["DSC_0013"]

    v.filter_var.set("All shots")
    v.apply_filter()
    v.show_single(0)
    assert v.mode == "single"
    assert v.file is not None and v.file.path.name == "DSC_0001.JPG"
    assert pump(root, lambda: v.large_photo is not None)
    assert "900 × 600" in v.info.cget("text")

    v.step(1)
    assert v.file.path.name == "DSC_0002.JPG"
    raw = next(f for f in v.shown[1].files if f.kind == "raw")
    v.show_file(raw)
    assert pump(root, lambda: "embedded preview" in v.info.cget("text"))

    v.jump(len(v.shown) - 1)
    assert v.file.kind == "video"

    v.close()
    assert closed == [True]


def test_viewer_without_pillow_lists_files(root, event, fmt, monkeypatch):
    from raw_jpg_import import previews
    from raw_jpg_import.gui.viewer import PhotoViewer

    monkeypatch.setattr(previews, "_pil_checked", True)
    monkeypatch.setattr(previews, "_pil", None)
    v = PhotoViewer(root, [event], fmt)
    v.withdraw()
    assert pump(root, lambda: len(v.shots) == 14)
    assert "Pillow" in v.notice.cget("text")
    v.show_single(0)
    assert v.file is not None and v.thumbs == {}
    v.close()


def test_app_opens_one_viewer_and_reuses_it(root, event, tmp_path, monkeypatch):
    from raw_jpg_import import engine
    from raw_jpg_import.gui.app import App

    monkeypatch.setattr(engine, "detect_cards", lambda: [])
    app = App(root, poll_cards=False)
    try:
        other = tmp_path / "2026.10.19 - other"
        _put(other / "JPG" / "X.JPG", _jpeg(100, 100, (1, 2, 3)), 1_700_000_000)
        app.open_viewer([event, other])
        first = app.viewer
        first.withdraw()
        app.open_viewer([event, other], other)
        assert app.viewer is first and first.folder == other
        assert pump(root, lambda: [s.name for s in first.shots] == ["X"])
        first.close()
        assert app.viewer is None
    finally:
        app.destroy()
