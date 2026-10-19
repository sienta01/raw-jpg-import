"""Regression tests for the GUI review findings (stale plans, layout, geometry, platform).

The model-level tests run everywhere. The window tests are marked ``gui`` (run them with
``pytest -m gui tests/gui``); card detection is replaced so only fake cards in ``tmp_path``
are ever seen.
"""

from __future__ import annotations

import gc
import os
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pytest
from fakes.files import write_file
from fakes.jpeg import make_jpeg
from fakes.raw import make_nef

from raw_jpg_import import engine, volumes
from raw_jpg_import.config import load_config
from raw_jpg_import.gui import viewmodel as vm
from raw_jpg_import.gui.viewmodel import AppState, SetupModel

try:
    import tkinter as tk
except ImportError:            # the model tests still run
    tk = None  # type: ignore[assignment]

DAY1 = date(2026, 1, 23)
DAY2 = date(2026, 1, 24)
SHOTS = [
    ("DSC_0001", datetime(2026, 1, 23, 17, 42, 0)),
    ("DSC_0002", datetime(2026, 1, 23, 18, 5, 30)),
    ("DSC_0003", datetime(2026, 1, 24, 10, 0, 0)),
]


def make_card(root: Path, shots=SHOTS) -> Path:
    for stem, when in shots:
        folder = root / "DCIM" / "100NCZ_F"
        write_file(folder / f"{stem}.NEF", make_nef(when, name=f"{stem}.NEF"), when)
        write_file(folder / f"{stem}.JPG", make_jpeg(when, name=f"{stem}.JPG"), when)
    return root


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# ---- model level (no display) ---------------------------------------------------------------


@pytest.fixture
def cfg():
    return load_config(use_user=False)


@pytest.fixture
def model(tmp_path, cfg, monkeypatch) -> SetupModel:
    monkeypatch.setattr(volumes, "_mounts", lambda: [])
    m = SetupModel()
    m.folder_order = cfg.formats.folders()
    entry = m.add_folder(make_card(tmp_path / "card"))
    m.scan_finished(entry.key, engine.scan_source(entry.source, cfg))
    m.rebuild()
    return m


def test_checking_disables_every_input_and_says_why():
    en = vm.enabled_actions(AppState.IDLE, scanning=False, dest_set=True, plan=None,
                            plan_pending=True, checking=True)
    assert en == vm.Enabled(False, False, False, False, "Checking the destination...")


def test_inputs_change_with_every_choice(model, tmp_path):
    before = model.inputs()
    assert model.inputs() == before
    model.set_name(DAY1, "Sunset Walk")
    assert model.inputs() != before
    after_name = model.inputs()
    model.set_checked(DAY2, False)
    assert model.inputs() != after_name
    after_tick = model.inputs()
    model.set_dest(str(tmp_path / "other"))
    assert model.inputs() != after_tick


def test_a_result_for_the_old_destination_does_not_hide_days_of_the_new_one(model, cfg,
                                                                           tmp_path):
    """Finding 5: a plan computed for the previous destination must not decide auto-hide."""
    lib = tmp_path / "Photos"
    lib.mkdir()
    new_lib = tmp_path / "Photos 2026"
    new_lib.mkdir()
    model.set_dest(str(lib))
    stale = vm.compute_plan(model.plan_request(cfg))
    stale.new_by_day = {DAY1: 0, DAY2: 0}       # as if both days were imported into Photos
    model.set_dest(str(new_lib))
    assert not model.is_current(stale)
    assert model.apply_plan(stale) is False
    assert model.hidden == set()
    assert model.is_checked(DAY1) and model.is_checked(DAY2)
    assert model.plan is None                    # nothing of the old destination was taken
    assert model.pending_auto_hide              # the next (current) result decides
    fresh = vm.compute_plan(model.plan_request(cfg))
    assert model.is_current(fresh)
    assert model.apply_plan(fresh) is False
    assert model.plan is not None and len(model.plan.to_copy) == 6
    assert model.hidden == set()


def test_existing_events_get_the_day_start(model, cfg, tmp_path, monkeypatch):
    seen = {}
    real = engine.existing_events

    def spy(dest_root, **kw):
        seen.update(kw)
        return real(dest_root, **kw)

    monkeypatch.setattr(engine, "existing_events", spy)
    lib = tmp_path / "lib"
    lib.mkdir()
    model.set_dest(str(lib))
    model.set_day_start(dtime(4, 0))
    vm.compute_plan(model.plan_request(cfg))
    assert seen["day_start"] == dtime(4, 0)


# ---- the window -----------------------------------------------------------------------------


def pump(root, until: Callable[[], bool], timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the window")


@pytest.fixture(scope="module")
def collect_on_the_tk_thread():
    """Only collect garbage on the Tk thread (see test_gui_smoke.py)."""
    was = gc.isenabled()
    gc.disable()
    yield
    gc.collect()
    if was:
        gc.enable()


def new_tk():
    if tk is None:
        pytest.skip("no tkinter")
    error = None
    for _ in range(4):
        gc.collect()
        try:
            return tk.Tk()
        except tk.TclError as e:
            error = e
            time.sleep(0.2)
    pytest.skip(f"cannot create a Tk window: {error}")


@pytest.fixture
def root(collect_on_the_tk_thread):
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
    m = app.model
    return bool(m.sources) and not m.scanning and app.state is AppState.IDLE


def import_ready(app) -> bool:
    m = app.model
    return m.plan is not None and not m.plan_pending and m.enabled(app.state).import_


def settled(app) -> bool:
    return scanned(app) and app.model.plan is not None and not app.model.plan_pending \
        and not app.model.checking


def ready_to_import(app, root, card: Path, dest: Path) -> None:
    app.setup.add_folder(card)
    pump(root, lambda: scanned(app) and bool(app.setup._rows))
    app.setup.set_dest(str(dest))
    app.setup.set_name(DAY1, "Sunset Wlak")
    pump(root, lambda: import_ready(app))


def disabled(widget) -> bool:
    return "disabled" in widget.state()


@pytest.mark.gui
def test_inputs_are_locked_while_the_final_plan_is_built(app, root, tmp_path):
    """Finding 4: after clicking Import nothing can be edited until the answer is in."""
    card = make_card(tmp_path / "card")
    dest = tmp_path / "dest"
    dest.mkdir()
    ready_to_import(app, root, card, dest)
    setup = app.setup
    setup.start_import()
    assert app.model.checking
    assert setup.status_label.cget("text") == "Checking the destination..."
    for w in (setup.dest_entry, setup.browse_btn, setup.day_start_box, setup.hide_btn,
              setup.merge_btn, setup.add_btn, setup.rescan_btn, setup.import_btn,
              setup.preview_btn, setup.settings_btn):
        assert disabled(w), w
    for row in setup._rows.values():
        assert disabled(row.check_btn) and disabled(row.combo) and disabled(row.join_btn)
    for widget, _ in setup._source_widgets:
        assert disabled(widget)
    pump(root, lambda: app.report is not None and not app.runner.busy)
    assert "2026.01.23 - Sunset Wlak/RAW/DSC_0001.NEF" in files_under(dest)
    app.run_again()
    pump(root, lambda: settled(app))
    setup.set_hide_imported(False)
    pump(root, lambda: settled(app) and len(setup._rows) == 2)
    assert not disabled(setup.dest_entry)
    assert all(not disabled(w.check_btn) for w in setup._rows.values())
    assert all(not disabled(widget) for widget, _ in setup._source_widgets)


@pytest.mark.gui
def test_a_change_while_checking_is_never_imported_with_the_old_plan(app, root, tmp_path):
    """Finding 4: an edit that lands while the final plan runs drops that plan."""
    from raw_jpg_import.gui.setup_page import CHANGED_WHILE_CHECKING

    card = make_card(tmp_path / "card")
    old = tmp_path / "old_lib"
    old.mkdir()
    new = tmp_path / "new_lib"
    new.mkdir()
    ready_to_import(app, root, card, old)
    setup = app.setup
    started: list[str] = []
    real = app.runner.run_light

    def spy(tag, fn, gen, *a, **kw):
        if app.state is AppState.IMPORTING:
            started.append(tag)
        return real(tag, fn, gen, *a, **kw)

    app.runner.run_light = spy
    setup.start_import()
    # Edits that still reach the model (a card event, a script): name, tick, destination.
    setup.set_name(DAY1, "Sunset Walk")
    app.model.set_checked(DAY2, False)
    setup.refresh_days()
    setup.set_dest(str(new))
    pump(root, lambda: settled(app))
    assert app.report is None and app.state is AppState.IDLE    # the old plan was dropped
    assert files_under(old) == set() and files_under(new) == set()
    assert setup.status_label.cget("text") == CHANGED_WHILE_CHECKING
    assert app.settings.dest_root == str(new)

    setup.start_import()                      # the user looks again and clicks Import
    pump(root, lambda: app.report is not None and not app.runner.busy)
    assert app.report.plan.dest_root == new
    assert files_under(new) == {
        "2026.01.23 - Sunset Walk/RAW/DSC_0001.NEF", "2026.01.23 - Sunset Walk/JPG/DSC_0001.JPG",
        "2026.01.23 - Sunset Walk/RAW/DSC_0002.NEF", "2026.01.23 - Sunset Walk/JPG/DSC_0002.JPG",
    }
    assert files_under(old) == set()
    assert app.settings.dest_root == str(new)
    assert started == []                      # no plan job ran during the import


@pytest.mark.gui
def test_a_declined_import_unlocks_the_inputs(app, root, tmp_path):
    card = make_card(tmp_path / "card")
    dest = tmp_path / "dest"
    dest.mkdir()
    app.settings.ask_before_import = True
    asked = []
    app.confirm = lambda title, message: asked.append(message) or False
    ready_to_import(app, root, card, dest)
    app.setup.start_import()
    pump(root, lambda: bool(asked) and not app.model.checking)
    assert app.report is None and files_under(dest) == set()
    assert not disabled(app.setup.dest_entry) and not disabled(app.setup.import_btn)


@pytest.mark.gui
def test_a_live_plan_for_the_old_destination_does_not_hide_new_days(app, root, tmp_path):
    """Finding 5: typing on after a plan of the previous destination started."""
    card = make_card(tmp_path / "card")
    lib = tmp_path / "Photos"
    lib.mkdir()
    lib2 = tmp_path / "Photos 2026"
    lib2.mkdir()
    ready_to_import(app, root, card, lib)
    app.setup.start_import()
    pump(root, lambda: app.report is not None and not app.runner.busy)
    app.run_again()
    pump(root, lambda: settled(app))
    assert app.model.hidden == {DAY1, DAY2}

    threads: list[threading.Thread] = []
    real = app.runner.run_light

    def spy(tag, fn, gen, *a, **kw):
        t = real(tag, fn, gen, *a, **kw)
        threads.append(t)
        return t

    app.runner.run_light = spy
    setup = app.setup
    setup.set_dest(str(lib) + os.sep)         # the user pauses: a plan of Photos starts
    setup._cancel_timer()
    setup._start_plan()
    threads[-1].join(30)                      # its result is waiting to be dispatched
    setup.set_dest(str(lib2))                 # ... and types " 2026"
    pump(root, lambda: settled(app) and app.model.plan.dest_root == lib2)
    m = app.model
    assert m.hidden == set()
    assert m.is_checked(DAY1) and m.is_checked(DAY2)
    assert setup.hidden_label.cget("text") == ""
    assert len(m.plan.to_copy) == 6


def parse_geometry(text: str) -> tuple[int, int, int, int]:
    size, x, y = text.replace("+-", "+~").split("+")
    w, h = size.split("x")
    return int(w), int(h), int(x.replace("~", "-")), int(y.replace("~", "-"))


@pytest.mark.gui
@pytest.mark.parametrize("where", ["right", "left", "below"])
def test_a_position_on_a_monitor_that_is_gone_is_not_restored(root, where):
    """Findings 7 and 17: the window must not open outside every screen."""
    from raw_jpg_import.gui.app import _apply_geometry

    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    far = {"right": f"+{sw + 30000}+120", "left": "+-30000+120", "below": f"+40+{sh + 30000}"}
    _apply_geometry(root, "1100x780" + far[where])
    root.update_idletasks()
    w, h, x, y = parse_geometry(root.geometry())
    assert 0 <= x < sw and 0 <= y < sh
    assert x + w // 2 < sw                      # the title bar's middle is on the screen
    assert w <= sw and h <= sh


@pytest.mark.gui
def test_a_visible_position_is_kept_and_a_huge_size_is_shrunk_when_moved(root):
    from raw_jpg_import.gui.app import _apply_geometry, _on_screen

    _apply_geometry(root, "1000x700+40+50")
    root.update_idletasks()
    assert parse_geometry(root.geometry()) == (1000, 700, 40, 50)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    _apply_geometry(root, f"{sw * 2}x{sh * 2}+{sw + 30000}+0")
    root.update_idletasks()
    w, h, x, y = parse_geometry(root.geometry())
    assert w <= sw and h <= sh and 0 <= x < sw and 0 <= y < sh
    # The check used where Windows cannot be asked (Tk's screen and virtual root).
    assert _on_screen(root, 10, 10, win32=False)
    assert not _on_screen(root, sw + 30000, 10, win32=False)
    assert not _on_screen(root, -30000, 10, win32=False)


def fully_visible(dlg, widget) -> bool:
    for _ in range(10):
        dlg.update()
    x = widget.winfo_rootx() - dlg.winfo_rootx()
    y = widget.winfo_rooty() - dlg.winfo_rooty()
    return (widget.winfo_ismapped() and widget.winfo_height() > 2 and widget.winfo_width() > 2
            and x + widget.winfo_width() <= dlg.winfo_width()
            and y + widget.winfo_height() <= dlg.winfo_height())


def buttons_of(widget, out=None) -> list:
    out = [] if out is None else out
    for c in widget.winfo_children():
        if c.winfo_class() in ("TButton", "TCheckbutton"):
            out.append(c)
        buttons_of(c, out)
    return out


@pytest.mark.gui
@pytest.mark.parametrize("scaling", [2.0, 2.333])          # Windows 150 % and 175 %
def test_dialog_buttons_are_visible_at_high_display_scaling(root, scaling):
    """Finding 20: fixed pixel sizes used to clip OK / Close / Delete at 150-175 %."""
    from types import SimpleNamespace

    from raw_jpg_import.deleter import DeletionPlan
    from raw_jpg_import.gui import dialogs
    from raw_jpg_import.gui.viewmodel import PreviewRow

    root.tk.call("tk", "scaling", scaling)
    show_offscreen(root)
    card = SimpleNamespace(label="NIKON Z F")
    excluded = [(SimpleNamespace(source=card, rel=f"DCIM/100NCZ_F/DSC_{i:04d}.NEF"),
                 "changed since the import") for i in range(30)]
    dplan = DeletionPlan(candidates=[object()] * 40, excluded=excluded, bytes=10**9,
                         per_source={"NIKON Z F": (40, 10**9)})
    rows = [PreviewRow("Copy", f"NIKON Z F: DSC_{i:04d}.NEF", "x", "", "1 MB")
            for i in range(50)]
    cfg = load_config(use_user=False)
    built = [
        dialogs.DeleteDialog(root, dplan),
        dialogs.TextDialog(root, "Unknown file types", "a\n" * 80,
                           intro="These files are not a photo or video type the app knows. "
                                 "They stay on the cards. To import them, add their type in "
                                 "the config file (Settings, File types)."),
        dialogs.ErrorDialog(root, "Import failed", "The import stopped: boom",
                            "Traceback (most recent call last):\n" + "  line\n" * 60),
        dialogs.SettingsDialog(root, dialogs.GeneralSettings("", "00:00", True, True), cfg,
                               None, on_apply=lambda g: None, on_reload=lambda: (cfg, None)),
        dialogs.PreviewDialog(root, "Preview", rows, on_import=lambda: None),
    ]
    for dlg in built:
        assert dlg.present()
        notebooks = [c for c in dlg.body.winfo_children() if c.winfo_class() == "TNotebook"]
        tabs = notebooks[0].tabs() if notebooks else [None]
        for tab in tabs:                    # every Settings tab in turn
            if tab is not None:
                notebooks[0].select(tab)
            dlg.update()
            shown = [b for b in buttons_of(dlg) if b.winfo_ismapped()]
            assert shown
            for b in shown:
                assert fully_visible(dlg, b), (type(dlg).__name__, b.cget("text"))
        if isinstance(dlg, dialogs.DeleteDialog):
            assert fully_visible(dlg, dlg.delete_button)
            assert fully_visible(dlg, dlg.understood_box)
        if isinstance(dlg, dialogs.SettingsDialog):
            assert fully_visible(dlg, dlg.ok_button)
        dlg.close()


@pytest.mark.gui
def test_a_year_of_days_builds_only_the_rows_in_view_and_keeps_them(app, root, tmp_path):
    """Finding 21: 365 days used to create ~3,000 widgets (seconds of 'Not responding')."""
    from raw_jpg_import.gui.setup_page import FIRST_ROWS

    start = datetime(2025, 1, 1, 12, 0, 0)
    folder = tmp_path / "old card copy" / "DCIM" / "100NCZ_F"
    for n in range(365):
        when = start + timedelta(days=n)
        write_file(folder / f"DSC_{n + 1:04d}.JPG", make_jpeg(when), when)
    show_offscreen(root)
    setup = app.setup
    setup.add_folder(tmp_path / "old card copy")
    pump(root, lambda: scanned(app) and bool(setup._rows), 120)
    for _ in range(5):
        root.update()
    assert len(setup.days.inner.winfo_children()) < 365       # not a row of widgets per day
    assert FIRST_ROWS <= len(setup._pool) < 100
    assert setup.days.inner.winfo_reqheight() >= 365 * setup._row_h     # scrolls the whole year

    days = [g.day for g in app.model.groups]
    setup.days.canvas.yview_moveto(1.0)                       # the user drags to the end
    for _ in range(10):
        root.update()
    last = setup._pool[days[-1]]
    assert int(last.combo.grid_info()["row"]) == 365
    assert last.name.get() == ""
    assert len(setup._pool) < 150

    setup.set_name(days[-1], "New Year's Eve")
    assert app.model.names_by_date[days[-1]] == "New Year's Eve"
    first = setup._pool[days[0]]
    setup.set_hide_imported(False)
    setup.set_hide_imported(True)
    root.update()
    assert setup._pool[days[0]] is first                      # kept, not rebuilt
    assert last.name.get() == "New Year's Eve"


def test_wheel_events_follow_the_tk_version():
    """Finding 27: X11 sends <MouseWheel> from Tk 8.7 on (TIP 474), Button-4/5 before."""
    pytest.importorskip("tkinter")
    from raw_jpg_import.gui.widgets import wheel_sequences

    assert wheel_sequences("x11", "8.6.13") == ("<Button-4>", "<Button-5>")
    assert wheel_sequences("x11", "8.7a5") == ("<MouseWheel>",)
    assert wheel_sequences("x11", "9.0.1") == ("<MouseWheel>",)
    assert wheel_sequences("win32", "8.6.12") == ("<MouseWheel>",)
    assert wheel_sequences("aqua", "8.6.13") == ("<MouseWheel>",)


@pytest.mark.gui
def test_the_wheel_scrolls_on_x11_with_tk_9(root, monkeypatch):
    import sys

    from raw_jpg_import.gui import widgets

    show_offscreen(root, "400x300")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(widgets, "_wheel_env", lambda w: ("x11", "9.0.1"), raising=False)
    area = widgets.ScrollableFrame(root)
    area.pack(fill="both", expand=True)
    for i in range(100):
        tk.Label(area.inner, text=f"row {i}").grid(row=i, column=0)
    for _ in range(5):
        root.update()
    assert area.canvas.yview()[0] == 0.0
    area.event_generate("<Enter>")
    area.inner.event_generate("<MouseWheel>", delta=-120)
    root.update()
    assert area.canvas.yview()[0] > 0.0


@pytest.mark.parametrize("orig", ["/usr/local/lib", None])
def test_xdg_open_from_the_frozen_linux_app_gets_the_system_libraries(monkeypatch, orig):
    """Finding 28: no PyInstaller library path in the file manager / editor we start."""
    pytest.importorskip("tkinter")
    import sys

    from raw_jpg_import.gui import widgets

    bundle = "/tmp/_MEIabc123"
    started = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", bundle, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{bundle}:{orig}" if orig else bundle)
    if orig:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", orig)
    else:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("TCL_LIBRARY", f"{bundle}/_tcl_data")
    monkeypatch.setenv("TK_LIBRARY", f"{bundle}/_tk_data")
    monkeypatch.setenv("HOME", "/home/me")
    monkeypatch.setattr(widgets.subprocess, "Popen",
                        lambda args, **kw: started.append((args, kw)))
    widgets.open_path("/home/me/Photos/2026.01.23 - Sunset Walk")
    [(args, kw)] = started
    assert args == ["xdg-open", "/home/me/Photos/2026.01.23 - Sunset Walk"]
    env = kw["env"]
    assert env.get("LD_LIBRARY_PATH") == orig
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert "TCL_LIBRARY" not in env and "TK_LIBRARY" not in env
    assert env["HOME"] == "/home/me"


def test_programs_started_from_python_inherit_the_environment(monkeypatch):
    pytest.importorskip("tkinter")
    import sys

    from raw_jpg_import.gui import widgets

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert widgets.child_env() is None


def many_days(n: int, per_day: int = 3) -> list[tuple[str, datetime]]:
    start = datetime(2026, 8, 20, 12, 0, 0)
    return [(f"DSC_{d * 10 + k + 1:04d}", start + timedelta(days=d, minutes=k))
            for d in range(n) for k in range(per_day)]


def show_offscreen(root, size: str = "1100x780") -> None:
    """Map the window outside the screen so geometry is real but nothing flashes."""
    root.geometry(f"{size}+-4000+0")
    root.deiconify()
    for _ in range(20):
        root.update()


@pytest.mark.gui
def test_summary_buttons_stay_visible_and_the_rest_scrolls(app, root, tmp_path):
    """Finding 6: 19 folders plus 25 kept files used to push the buttons off the window."""
    card = make_card(tmp_path / "card", many_days(19))
    dest = tmp_path / "dest"
    dest.mkdir()
    app.setup.add_folder(card)
    pump(root, lambda: scanned(app) and bool(app.setup._rows))
    app.setup.set_dest(str(dest))
    pump(root, lambda: import_ready(app))
    app.setup.start_import()
    pump(root, lambda: app.report is not None and not app.runner.busy)
    assert len(app.report.folders) == 19
    for p in sorted((card / "DCIM" / "100NCZ_F").iterdir())[:25]:      # changed: kept
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    app.begin_delete(engine.plan_deletion(app.report))
    pump(root, lambda: app.dreport is not None and not app.runner.busy)
    assert "Kept on cards: 25" in app.summary.deletion.cget("text")

    show_offscreen(root)
    page = app.summary
    bar = page.bar
    assert bar.winfo_height() > 1
    assert bar.winfo_y() + bar.winfo_height() <= page.winfo_height()
    assert page.content._can_scroll()
    for child in bar.winfo_children():
        assert child.winfo_ismapped() and child.winfo_height() > 1

