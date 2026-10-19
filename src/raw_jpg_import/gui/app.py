"""The desktop window: ``python -m raw_jpg_import`` (no arguments) or ``raw-jpg-import-gui``.

:class:`App` owns the state machine (IDLE, SCANNING, IMPORTING, DELETING), the one long
job at a time (scan queue, import, delete) and the card polling; the pages are views.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:            # Python built without Tk (some Linux distributions, Homebrew)
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

from .. import engine
from ..config import Config, load_config
from ..errors import AnotherImportRunning, Cancelled, ConfigError, PlanBlocked
from ..events import FileDone, Notice, Progress
from ..settings import Settings, load_settings, save_settings
from .viewmodel import AppState, SetupModel, fmt_hhmm, parse_hhmm

if TYPE_CHECKING:
    from ..copier import ImportReport
    from ..deleter import DeletionPlan, DeletionReport
    from ..planner import ImportPlan
    from .dialogs import GeneralSettings
    from .jobs import JobRunner

log = logging.getLogger(__name__)

TITLE = "Raw/JPG Import"
MIN_SIZE = (900, 600)
DEFAULT_SIZE = "1100x780"
POLL_MS = 3000
_SAVED_GEOMETRY = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")     # what root.geometry() gives

TK_HINT = """\
Raw/JPG Import needs Tkinter, which this Python does not include.
  Debian/Ubuntu:  sudo apt install python3-tk
  Fedora:         sudo dnf install python3-tkinter
  macOS Homebrew: brew install python-tk
The command line works without it: raw-jpg-import --help"""


def load_cfg() -> tuple[Config, str | None]:
    """The effective config, or the built-in one plus the error when the user's is bad."""
    try:
        cfg = engine.load_config()
    except ConfigError as e:
        log.error("config error: %s", e)
        return load_config(use_user=False), str(e)
    for w in cfg.warnings:
        log.warning("config: %s", w)
    return cfg, None


class App:
    def __init__(self, root: tk.Tk, *, settings: Settings | None = None,
                 poll_cards: bool = True) -> None:
        from . import dialogs
        from .jobs import JobRunner
        from .progress_page import ProgressPage
        from .setup_page import SetupPage
        from .summary_page import SummaryPage

        self._dialogs = dialogs
        self.root = root
        self.settings = settings if settings is not None else load_settings()
        self.cfg, self.cfg_error = load_cfg()
        self.state = AppState.IDLE
        self.closing = False
        self.destroyed = False
        self.report: ImportReport | None = None
        self.dreport: DeletionReport | None = None
        self.viewer: Any = None                  # the photo viewer window, when open
        self.scan_key: str | None = None
        self._poll_running = False
        self._poll_after: str | None = None
        self.errors: list[str] = []          # unexpected callback errors (shown and logged)
        self.confirm = self._confirm
        self.runner: JobRunner = JobRunner(root, self.dispatch)
        s = self.settings
        self.model = SetupModel(day_start=parse_hhmm(s.day_start),
                                hide_imported=s.hide_imported_days,
                                recent_names=s.recent_names, dest_text=s.dest_root or "")
        self._apply_cfg_to_model()

        container = ttk.Frame(root)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self.setup = SetupPage(container, self)
        self.progress = ProgressPage(container, self)
        self.summary = SummaryPage(container, self)
        for page in (self.setup, self.progress, self.summary):
            page.grid(row=0, column=0, sticky="nsew")
        self.page: ttk.Frame = self.setup
        self.show_page(self.setup)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._on_tk_error  # type: ignore[method-assign]
        if poll_cards:
            self.poll_cards()
        self.setup.schedule_plan(0)

    # ---- small helpers ------------------------------------------------------------------------

    def _confirm(self, title: str, message: str) -> bool:
        return self._dialogs.confirm(self.root, title, message)

    def show_error(self, title: str, message: str, details: str = "") -> None:
        if self.closing:
            return
        self._dialogs.show_error(self.root, title, message, details)

    def save_settings(self) -> None:
        try:
            save_settings(self.settings)
        except OSError as e:
            log.error("could not save settings: %s", e)

    def show_page(self, page: ttk.Frame) -> None:
        self.page = page
        page.tkraise()

    def set_state(self, state: AppState) -> None:
        self.state = state
        self._apply_state()

    def _apply_state(self) -> None:
        self.setup.apply_state()

    def can_plan(self) -> bool:
        return not self.closing and self.state in (AppState.IDLE, AppState.SCANNING)

    def _apply_cfg_to_model(self) -> None:
        fmt = self.cfg.formats
        self.model.folder_order = fmt.folders() + [fmt.other_folder]
        self.model.config_error = self.cfg_error

    def _on_tk_error(self, exc: type[BaseException], value: BaseException, tb: Any) -> None:
        import traceback

        details = "".join(traceback.format_exception(exc, value, tb))
        log.error("unexpected error in the window:\n%s", details)
        self.errors.append(details)
        self.show_error("Unexpected error", f"Something went wrong: {value}", details)

    # ---- events from workers ------------------------------------------------------------------

    def dispatch(self, ev: object) -> None:
        from .jobs import JobDone, JobFailed, LightDone, LightFailed

        if self.closing and not isinstance(ev, (JobDone, JobFailed)):
            return
        if isinstance(ev, LightDone):
            if ev.tag == "cards":
                self.on_cards(ev.result)
            elif ev.tag == "plan":
                self.setup.on_plan_done(ev.gen, ev.result)
            elif ev.tag == "final":
                self.setup.on_final_plan(ev.gen, ev.result)
        elif isinstance(ev, LightFailed):
            if ev.tag == "cards":
                self._poll_running = False
                log.warning("card detection failed: %s", ev.error)
            else:
                self.setup.on_plan_failed(ev.gen, ev.error, ev.details)
        elif isinstance(ev, Progress):
            if self.state is AppState.SCANNING:
                self.setup.on_scan_progress(self.scan_key, ev)
            elif self.state in (AppState.IMPORTING, AppState.DELETING):
                self.progress.on_progress(ev)
        elif isinstance(ev, FileDone):
            if self.state in (AppState.IMPORTING, AppState.DELETING):
                self.progress.on_file_done(ev)
        elif isinstance(ev, Notice):
            if self.state in (AppState.IMPORTING, AppState.DELETING):
                self.progress.on_notice(ev)
            else:
                log.info("%s: %s", ev.level, ev.text)
        elif isinstance(ev, JobDone):
            {"scan": self._scan_done, "import": self._import_done,
             "delete": self._delete_done}[ev.kind](ev.result)
        elif isinstance(ev, JobFailed):
            {"scan": self._scan_failed, "import": self._import_failed,
             "delete": self._delete_failed}[ev.kind](ev.error, ev.details)

    # ---- cards and scanning -------------------------------------------------------------------

    def poll_cards(self) -> None:
        self._poll_after = None
        if self.closing:
            return
        if not self._poll_running and self.state in (AppState.IDLE, AppState.SCANNING):
            self._poll_running = True
            self.runner.run_light("cards", engine.detect_cards, 0)
        self._poll_after = self.root.after(POLL_MS, self.poll_cards)

    def on_cards(self, volumes: list[engine.Volume]) -> None:
        self._poll_running = False
        if not self.can_plan():
            return
        changes = self.model.cards_changed(volumes)
        if not changes:
            return
        for e in changes.removed:
            self.cancel_scan_of(e.key)
        self.setup.sources_changed()
        self.queue_scans()

    def queue_scans(self) -> None:
        """Start scanning the next waiting source, one at a time (only on the setup page)."""
        if self.runner.busy or self.closing or self.page is not self.setup:
            return
        entry = self.model.next_to_scan()
        if entry is None:
            if self.state is AppState.SCANNING:
                self.set_state(AppState.IDLE)
            return
        entry.status = "scanning"
        entry.detail = ""
        self.scan_key = entry.key
        self.set_state(AppState.SCANNING)
        self.setup.refresh_sources()
        self.runner.start("scan", engine.scan_source, entry.source, self.cfg)

    def cancel_scan_of(self, source_key: str) -> None:
        if self.state is AppState.SCANNING and self.scan_key == source_key:
            self.runner.request_cancel()

    def rescan(self) -> None:
        if self.state is AppState.SCANNING:
            self.runner.request_cancel()
        self.model.mark_for_rescan()
        self.setup.refresh_sources()
        self.queue_scans()

    def _scan_done(self, scanned: engine.ScannedSource) -> None:
        key, self.scan_key = self.scan_key, None
        entry = self.model.find(key) if key else None
        if entry is not None:
            if entry.present and entry.checked:
                self.model.scan_finished(entry.key, scanned)
            else:
                entry.status = "waiting"
        self.set_state(AppState.IDLE)
        self.setup.sources_changed()
        self.queue_scans()

    def _scan_failed(self, error: BaseException, details: str) -> None:
        key, self.scan_key = self.scan_key, None
        entry = self.model.find(key) if key else None
        if entry is not None:
            if isinstance(error, Cancelled):
                entry.status = "waiting"
                entry.detail = ""
            else:
                log.error("scan of %s failed: %s\n%s", entry.source.root, error, details)
                self.model.scan_failed(entry.key, str(error) or type(error).__name__)
        self.set_state(AppState.IDLE)
        self.setup.refresh_sources()
        self.queue_scans()

    # ---- import -------------------------------------------------------------------------------

    def begin_import(self, plan: ImportPlan, names: list[str]) -> None:
        if self.runner.busy:
            self.show_error("Busy", "Please wait until the cards are scanned.")
            return
        self.settings.dest_root = str(plan.dest_root)
        self.settings.day_start = fmt_hhmm(plan.day_start)
        for name in reversed(names):
            self.settings.remember_name(name)
        self.save_settings()
        self.model.recent_names = list(self.settings.recent_names)
        self.set_state(AppState.IMPORTING)
        self.progress.begin_import(plan)
        self.show_page(self.progress)
        self.runner.start("import", engine.execute, plan)

    def _import_done(self, report: ImportReport) -> None:
        self.report = report
        self.dreport = None
        self.set_state(AppState.IDLE)
        if self.closing:
            return
        self.summary.show_report(report)
        self.show_page(self.summary)
        self.root.bell()

    def _import_failed(self, error: BaseException, details: str) -> None:
        self.set_state(AppState.IDLE)
        if self.closing:
            return
        self.show_page(self.setup)
        if isinstance(error, AnotherImportRunning):
            self.show_error("Import", str(error))
        elif isinstance(error, PlanBlocked):
            self.show_error("Import", "The import cannot start:\n" + "\n".join(error.problems))
        elif isinstance(error, Cancelled):
            pass
        else:
            log.error("import failed: %s\n%s", error, details)
            self.show_error("Import failed", f"The import stopped: {error}", details)
        self.setup.schedule_plan(0)

    # ---- delete -------------------------------------------------------------------------------

    def ask_delete(self) -> None:
        report = self.report
        if report is None or self.runner.busy:
            return
        dplan = engine.plan_deletion(report)
        if not dplan.candidates:
            self.show_error("Delete from cards", "Nothing can be deleted from the cards: no "
                                                 "file has a verified copy from this import.")
            return
        if self._dialogs.DeleteDialog(self.root, dplan).ask():
            self.begin_delete(dplan)

    def begin_delete(self, dplan: DeletionPlan) -> None:
        if self.runner.busy:
            return
        self.set_state(AppState.DELETING)
        self.progress.begin_delete(dplan, self.report)
        self.show_page(self.progress)
        log_path = self.report.log_path if self.report is not None else None
        self.runner.start("delete", engine.delete_originals, dplan, confirmed=True,
                          log_path=log_path)

    def _delete_done(self, dreport: DeletionReport) -> None:
        self.dreport = dreport
        self.set_state(AppState.IDLE)
        self.model.mark_for_rescan()          # the cards changed
        if self.closing:
            return
        self.summary.show_deletion(dreport)
        self.show_page(self.summary)
        self.root.bell()

    def _delete_failed(self, error: BaseException, details: str) -> None:
        self.set_state(AppState.IDLE)
        self.model.mark_for_rescan()
        if self.closing:
            return
        self.show_page(self.summary)
        if isinstance(error, AnotherImportRunning):
            self.show_error("Delete from cards", str(error))
        elif not isinstance(error, Cancelled):
            log.error("delete failed: %s\n%s", error, details)
            self.show_error("Delete failed", f"Deleting stopped: {error}", details)

    def cancel_job(self) -> None:
        self.runner.request_cancel()

    # ---- after an import ----------------------------------------------------------------------

    def run_again(self) -> None:
        """Back to the setup page with the same choices; rescan and re-plan."""
        self.show_page(self.setup)
        self.model.mark_for_rescan()
        self.setup.sources_changed()
        self.queue_scans()

    def new_import(self) -> None:
        """Back to the setup page with fresh day choices."""
        self.report = None
        self.dreport = None
        self.model.reset_days()
        self.setup.sync_controls()
        self.run_again()

    # ---- photo viewer -------------------------------------------------------------------------

    def open_viewer(self, folders: list[Path], start: Path | None = None) -> None:
        """Show the imported photos; one viewer window, reused and raised."""
        from .viewer import PhotoViewer

        if not folders:
            return
        viewer = self.viewer
        if viewer is not None:
            try:
                viewer.deiconify()
                viewer.lift()
                viewer.open_folder(start or folders[0])
                return
            except tk.TclError:
                self.viewer = None
        self.viewer = PhotoViewer(self.root, folders, self.cfg.formats, start,
                                  on_close=self._viewer_closed)

    def _viewer_closed(self) -> None:
        self.viewer = None

    # ---- settings -----------------------------------------------------------------------------

    def open_settings(self) -> None:
        g = self._dialogs.GeneralSettings(
            dest_root=self.settings.dest_root or "", day_start=fmt_hhmm(self.model.day_start),
            hide_imported=self.model.hide_imported,
            ask_before_import=self.settings.ask_before_import)
        self._dialogs.SettingsDialog(self.root, g, self.cfg, self.cfg_error,
                                     on_apply=self.apply_general,
                                     on_reload=self.reload_config).show()

    def apply_general(self, g: GeneralSettings) -> None:
        self.settings.dest_root = g.dest_root or None
        self.settings.day_start = g.day_start
        self.settings.hide_imported_days = g.hide_imported
        self.settings.ask_before_import = g.ask_before_import
        self.save_settings()
        self.setup.apply_general(g)

    def reload_config(self) -> tuple[Config, str | None]:
        cfg, error = load_cfg()
        changed = cfg.formats != self.cfg.formats
        self.cfg, self.cfg_error = cfg, error
        self._apply_cfg_to_model()
        if changed and self.can_plan():
            self.rescan()                 # files are classified while scanning
        self.setup.refresh_plan_views()
        self.setup.schedule_plan(0)
        return cfg, error

    # ---- closing ------------------------------------------------------------------------------

    def on_close(self) -> None:
        if self.state in (AppState.IMPORTING, AppState.DELETING):
            if not self.confirm("Cancel", self.progress.cancel_question()):
                return
            self.closing = True
            self.progress.set_cancelling()
            self.runner.request_cancel()
            self._destroy_when_idle()
            return
        self.closing = True
        self.runner.request_cancel()
        self.destroy()

    def _destroy_when_idle(self) -> None:
        if self.runner.busy:
            self.root.after(100, self._destroy_when_idle)
            return
        self.destroy()

    def destroy(self) -> None:
        if self.destroyed:
            return
        self.closing = True
        self.destroyed = True
        if self.viewer is not None:
            self.viewer.loader.close()
        try:
            if self.root.state() == "normal":
                self.settings.window_geometry = self.root.geometry()
                self.save_settings()
        except tk.TclError:
            pass
        if self._poll_after is not None:
            try:
                self.root.after_cancel(self._poll_after)
            except tk.TclError:
                pass
        try:
            self.setup.stop()
        except tk.TclError:
            pass
        self.runner.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---- startup ----------------------------------------------------------------------------------


def _dpi_aware() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def _apply_geometry(root: tk.Tk, geometry: str | None) -> None:
    root.minsize(*MIN_SIZE)
    try:
        root.geometry(_visible_geometry(root, geometry or DEFAULT_SIZE))
    except tk.TclError:
        root.geometry(DEFAULT_SIZE)


def _visible_geometry(root: tk.Misc, geometry: str) -> str:
    """``geometry``, unless its title bar would be on no screen.

    The saved position may belong to a monitor that is gone (a laptop taken off its dock).
    Then the window is centred on the main screen instead, no larger than that screen.
    """
    m = _SAVED_GEOMETRY.fullmatch(geometry.strip())
    if m is None:
        return geometry
    w, h, x, y = map(int, m.groups())
    title_bar = [(x + w // 2, y + 10), (x + 60, y + 10), (x + w - 60, y + 10)]
    if any(_on_screen(root, px, py) for px, py in title_bar):
        return geometry
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(w, sw), min(h, max(sh - 80, MIN_SIZE[1]))
    return f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}"


def _on_screen(root: tk.Misc, x: int, y: int, *, win32: bool = sys.platform == "win32") -> bool:
    """Whether the point is on a monitor (Windows asks the system; elsewhere Tk's screen)."""
    if win32:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.MonitorFromPoint.restype = ctypes.c_void_p
            user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            return bool(user32.MonitorFromPoint(wintypes.POINT(x, y), 0))  # DEFAULTTONULL
        except (OSError, AttributeError, ValueError):
            pass
    vx, vy = root.winfo_vrootx(), root.winfo_vrooty()
    vw, vh = root.winfo_vrootwidth(), root.winfo_vrootheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    return (vx <= x < vx + vw and vy <= y < vy + vh) or (0 <= x < sw and 0 <= y < sh)


def main() -> int:
    if tk is None:
        stream = sys.stderr or sys.__stderr__
        if stream is not None:
            print(TK_HINT, file=stream)
        return 1
    from ..report import configure_logging

    configure_logging(console=sys.stderr is not None)
    _dpi_aware()
    try:
        root = tk.Tk()
    except tk.TclError as e:
        if sys.stderr is not None:
            print(f"Cannot open a window ({e}). The command line works without one: "
                  "raw-jpg-import --help", file=sys.stderr)
        return 1
    root.title(TITLE)
    if sys.platform.startswith("linux"):
        try:
            ttk.Style(root).theme_use("clam")
        except tk.TclError:
            pass
    settings = load_settings()
    _apply_geometry(root, settings.window_geometry)
    App(root, settings=settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
