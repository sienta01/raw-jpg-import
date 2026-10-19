"""End-to-end smoke tests of the Tk window on fake cards (needs a display).

Run explicitly: ``pytest -m gui tests/gui/test_gui_smoke.py``. Card detection is replaced so
only the fake card folders in ``tmp_path`` are ever seen.
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest
from fakes.files import write_file
from fakes.jpeg import make_jpeg
from fakes.raw import make_nef

pytestmark = pytest.mark.gui

tk = pytest.importorskip("tkinter")

DAY1 = date(2026, 1, 23)
DAY2 = date(2026, 1, 24)
SHOTS = [
    ("DSC_0001", datetime(2026, 1, 23, 17, 42, 0)),
    ("DSC_0002", datetime(2026, 1, 23, 18, 5, 30)),
    ("DSC_0003", datetime(2026, 1, 24, 10, 0, 0)),
]


def make_card(root: Path) -> Path:
    """A folder laid out like a Zf card holding NEF+JPG pairs."""
    for stem, when in SHOTS:
        folder = root / "DCIM" / "100NCZ_F"
        write_file(folder / f"{stem}.NEF", make_nef(when, name=f"{stem}.NEF"), when)
        write_file(folder / f"{stem}.JPG", make_jpeg(when, name=f"{stem}.JPG"), when)
    return root


def card_files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def pump(root: tk.Tk, until: Callable[[], bool], timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the window")


@pytest.fixture(scope="module", autouse=True)
def collect_on_the_tk_thread():
    """Only collect garbage on this (the Tk) thread.

    pytest keeps each test's window alive until after teardown; if a worker thread of a
    later test then happened to free it, Tcl would be torn down on the wrong thread and
    the next ``Tk()`` could fail. So automatic collection is off for these tests and the
    fixtures collect explicitly.
    """
    was = gc.isenabled()
    gc.disable()
    yield
    gc.collect()
    if was:
        gc.enable()


def new_tk() -> tk.Tk:
    """``Tk()``, retried: a fresh Tcl interpreter in a process that already had several
    occasionally fails to read its own library files on this Windows Python."""
    error: tk.TclError | None = None
    for _ in range(4):
        gc.collect()                # the previous test's window, on this thread
        try:
            return tk.Tk()
        except tk.TclError as e:
            error = e
            time.sleep(0.2)
    pytest.skip(f"cannot create a Tk window: {error}")


@pytest.fixture
def root():
    r = new_tk()
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass
    gc.collect()


@pytest.fixture
def app(root, monkeypatch):
    from raw_jpg_import import engine, volumes
    from raw_jpg_import.gui.app import App
    from raw_jpg_import.settings import Settings

    monkeypatch.setattr(volumes, "_mounts", lambda: [])
    monkeypatch.setattr(engine, "detect_cards", lambda: [])
    a = App(root, settings=Settings(ask_before_import=False))
    a.confirm = lambda title, message: True
    yield a
    a.destroy()
    assert a.errors == []


def scanned(app) -> bool:
    from raw_jpg_import.gui.viewmodel import AppState

    m = app.model
    return bool(m.sources) and not m.scanning and app.state is AppState.IDLE


def import_ready(app) -> bool:
    m = app.model
    return m.plan is not None and not m.plan_pending and m.enabled(app.state).import_


def run_import(app, root, card: Path, dest: Path) -> None:
    app.setup.add_folder(card)
    pump(root, lambda: scanned(app) and bool(app.setup._rows))
    assert sorted(app.setup._rows) == [DAY1, DAY2]
    app.setup.set_dest(str(dest))
    app.setup.set_name(DAY1, "Sunset Walk")
    pump(root, lambda: import_ready(app))
    assert app.model.import_label() == "Import 6 files"
    assert app.setup.import_btn.cget("text") == "Import 6 files"
    app.setup.start_import()
    pump(root, lambda: app.report is not None and app.runner.busy is False)


def test_import_from_a_folder(app, root, tmp_path):
    card = make_card(tmp_path / "card")
    before = card_files(card)
    dest = tmp_path / "dest"
    dest.mkdir()
    run_import(app, root, card, dest)

    assert app.report.status == "ok"
    assert app.page is app.summary
    assert {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()} == {
        "2026.01.23 - Sunset Walk/RAW/DSC_0001.NEF", "2026.01.23 - Sunset Walk/JPG/DSC_0001.JPG",
        "2026.01.23 - Sunset Walk/RAW/DSC_0002.NEF", "2026.01.23 - Sunset Walk/JPG/DSC_0002.JPG",
        "2026.01.24/RAW/DSC_0003.NEF", "2026.01.24/JPG/DSC_0003.JPG",
    }
    assert card_files(card) == before
    assert app.settings.dest_root == str(dest)
    assert app.settings.recent_names[0] == "Sunset Walk"

    # Run again: both days are now fully imported, so they are hidden and Import is off.
    app.run_again()
    pump(root, lambda: scanned(app) and app.model.plan is not None
         and not app.model.plan_pending)
    assert app.page is app.setup
    assert app.model.hidden == {DAY1, DAY2}
    assert app.setup.hidden_label.cget("text") == "2 days already imported (hidden)"
    assert not app.model.enabled(app.state).import_


def test_delete_imported_files_from_the_fake_card(app, root, tmp_path):
    from raw_jpg_import import engine

    card = make_card(tmp_path / "card")
    dest = tmp_path / "dest"
    dest.mkdir()
    run_import(app, root, card, dest)
    dplan = engine.plan_deletion(app.report)
    assert len(dplan.candidates) == 6
    for c in dplan.candidates:      # never anything outside this test's folder
        assert c.src.path.resolve().is_relative_to(tmp_path.resolve())
    app.begin_delete(dplan)
    pump(root, lambda: app.dreport is not None and not app.runner.busy)
    assert app.dreport.status == "ok"
    assert card_files(card) == set()
    assert len([p for p in dest.rglob("*") if p.is_file()]) == 6
    assert "Deleted from cards: 6 files" in app.summary.deletion.cget("text")


def test_dialogs_build(app, root, tmp_path):
    from raw_jpg_import import engine
    from raw_jpg_import.gui import dialogs
    from raw_jpg_import.gui.viewmodel import plan_rows, result_rows

    card = make_card(tmp_path / "card")
    dest = tmp_path / "dest"
    dest.mkdir()
    run_import(app, root, card, dest)
    report = app.report
    built = [
        dialogs.PreviewDialog(root, "Preview", plan_rows(report.plan), on_import=lambda: None),
        dialogs.PreviewDialog(root, "Copied", result_rows(report, "copied")),
        dialogs.DeleteDialog(root, engine.plan_deletion(report)),
        dialogs.ErrorDialog(root, "Oops", "Something failed", "Traceback ..."),
        dialogs.TextDialog(root, "Details", "a\nb"),
        dialogs.SettingsDialog(root, dialogs.GeneralSettings(str(dest), "01:00", True, True),
                               app.cfg, None, on_apply=lambda g: None,
                               on_reload=lambda: (app.cfg, None)),
    ]
    root.update()
    preview = built[0]
    pump(root, lambda: len(preview.tree.get_children()) == len(report.plan.items))
    preview.filter.set("Copy")
    preview.refill()
    root.update()
    assert len(preview.tree.get_children()) == 6
    delete = built[2]
    assert "disabled" in delete.delete_button.state()
    delete.understood.set(True)
    delete._sync()
    assert "disabled" not in delete.delete_button.state()
    for d in built:
        d.close()


def test_closing_while_importing_cancels_and_waits(app, root, tmp_path):
    from raw_jpg_import.gui.viewmodel import AppState

    card = make_card(tmp_path / "card")
    before = card_files(card)
    dest = tmp_path / "dest"
    dest.mkdir()
    app.setup.add_folder(card)
    app.setup.set_dest(str(dest))
    pump(root, lambda: scanned(app) and import_ready(app))
    app.begin_import(app.model.plan, [])
    assert app.state is AppState.IMPORTING and app.page is app.progress
    app.on_close()                      # confirmed: cancel, wait for the worker, close
    assert app.closing
    pump(root, lambda: app.destroyed)
    assert not app.runner.busy
    assert app.report is not None       # the worker finished before the window closed
    assert not list(dest.rglob("*.part"))
    assert card_files(card) == before


def test_typing_survives_a_second_source(app, root, tmp_path):
    card = make_card(tmp_path / "card")
    app.setup.add_folder(card)
    pump(root, lambda: scanned(app) and bool(app.setup._rows))
    app.setup.set_name(DAY2, "Morning hike")
    other = tmp_path / "other"
    when = datetime(2026, 1, 25, 9, 0, 0)
    write_file(other / "DCIM" / "100NCZ_F" / "DSC_0100.JPG", make_jpeg(when), when)
    app.setup.add_folder(other)
    pump(root, lambda: scanned(app) and len(app.setup._rows) == 3)
    assert app.setup._rows[DAY2].name.get() == "Morning hike"
    assert app.model.names_by_date[DAY2] == "Morning hike"
