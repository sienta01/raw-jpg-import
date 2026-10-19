"""The first page: sources, destination, the days grid, and the Import button."""

from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from tkinter import filedialog, font, ttk
from typing import TYPE_CHECKING, Any

from ..errors import Cancelled
from ..events import CancelToken, Progress
from ..report import plural
from .dialogs import GeneralSettings, PreviewDialog, TextDialog
from .viewmodel import (
    DAY_STARTS,
    WARN,
    DayRow,
    PlanResult,
    compute_plan,
    confirm_text,
    fmt_hhmm,
    parse_hhmm,
    plan_rows,
    progress_text,
    scan_summary,
    short_path,
)
from .widgets import ERROR_FG, MUTED_FG, WARN_FG, ScrollableFrame, set_state

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

PLAN_DELAY_MS = 300
UP = "↑"
CHANGED_WHILE_CHECKING = "Something changed while checking: look again, then click Import"
# Each Tk widget is a native window, so a year of days is thousands of them: the first rows
# are built at once, the others only when they scroll into view, and rows are kept by day.
FIRST_ROWS = 40
BUILD_MARGIN = 10


@dataclass
class _RowWidgets:
    check: tk.BooleanVar
    join: tk.BooleanVar
    name: tk.StringVar
    check_btn: ttk.Checkbutton
    join_btn: ttk.Checkbutton
    combo: ttk.Combobox
    same: ttk.Label
    new: ttk.Label
    labels: list[ttk.Label] = field(default_factory=list)
    row: DayRow | None = None
    cells: list[tuple[tk.Misc, dict[str, Any]]] = field(default_factory=list)  # grid options
    grid_row: int | None = None       # None while not shown


class SetupPage(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=(12, 8, 12, 8))
        self.app = app
        self.model = app.model
        self.plan_gen = 0
        self._plan_after: str | None = None
        self._plan_cancel = CancelToken()
        self._final_inputs: tuple[object, ...] | None = None
        self._interrupted = False
        self._locked = False          # inputs disabled (final check, import, delete)
        self._syncing = False
        self._grid_key: tuple[Any, ...] | None = None
        self._rows: dict[date, _RowWidgets] = {}      # rows on screen (built and visible)
        self._pool: dict[date, _RowWidgets] = {}      # every built row, shown or not
        self._order: list[date] = []                  # visible days, top to bottom
        self._latest: dict[date, DayRow] = {}
        self._grid_cols: tuple[str, ...] | None = None
        self._header: list[ttk.Label] = []
        self._name_col = 0
        self._row_h = 0
        self._sized_rows = 0
        self._build_after: str | None = None
        self._status_labels: dict[str, ttk.Label] = {}
        self._source_vars: list[tk.BooleanVar] = []
        self._source_widgets: list[tuple[tk.Misc, bool]] = []   # (widget, enabled if unlocked)
        self._reimport_btn: ttk.Checkbutton | None = None
        self._edit_widgets: list[tk.Misc] = []
        self.bold = font.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.columnconfigure(0, weight=1)
        self._build_sources()
        self._build_destination()
        self._build_days()
        self._build_bottom()
        self.rowconfigure(2, weight=1)
        self.refresh_sources()
        self.refresh_days()
        self.refresh_plan_views()

    # ---- layout -------------------------------------------------------------------------------

    def _section(self, row: int, title: str) -> ttk.LabelFrame:
        box = ttk.LabelFrame(self, text=f" {title} ", padding=(8, 4, 8, 6))
        box.grid(row=row, column=0, sticky="nsew", pady=(0, 6))
        box.columnconfigure(0, weight=1)
        return box

    def _build_sources(self) -> None:
        box = self._section(0, "1  Sources")
        self.sources_frame = ttk.Frame(box)
        self.sources_frame.grid(row=0, column=0, sticky="ew")
        bar = ttk.Frame(box)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.add_btn = ttk.Button(bar, text="Add folder...", command=self._on_add_folder)
        self.add_btn.pack(side="left")
        self.rescan_btn = ttk.Button(bar, text="Rescan", command=self.app.rescan)
        self.rescan_btn.pack(side="left", padx=6)
        ttk.Label(bar, text="Watching for cards...", foreground=MUTED_FG).pack(side="right")
        self._edit_widgets += [self.add_btn, self.rescan_btn]

    def _build_destination(self) -> None:
        box = self._section(1, "2  Destination")
        box.columnconfigure(0, weight=0)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Import into:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.dest_var = tk.StringVar(value=self.model.dest_text)
        self.dest_entry = ttk.Entry(box, textvariable=self.dest_var)
        self.dest_entry.grid(row=0, column=1, sticky="ew")
        self.browse_btn = ttk.Button(box, text="Browse...", command=self._on_browse)
        self.browse_btn.grid(row=0, column=2, padx=(6, 0))
        self.dest_info = ttk.Label(box, text="", foreground=MUTED_FG)
        self.dest_info.grid(row=1, column=1, sticky="w", pady=(2, 0))
        self.dest_var.trace_add("write", self._on_dest_changed)
        self._edit_widgets += [self.dest_entry, self.browse_btn]

    def _build_days(self) -> None:
        box = self._section(2, "3  Days")
        controls = ttk.Frame(box)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Label(controls, text="Day starts at").pack(side="left")
        values = list(DAY_STARTS)
        current = fmt_hhmm(self.model.day_start)
        if current not in values:
            values.append(current)
        self.day_start_var = tk.StringVar(value=current)
        self.day_start_box = ttk.Combobox(controls, textvariable=self.day_start_var,
                                          values=values, state="readonly", width=6)
        self.day_start_box.pack(side="left", padx=(4, 14))
        self.day_start_box.bind("<<ComboboxSelected>>", self._on_day_start)
        self.hide_var = tk.BooleanVar(value=self.model.hide_imported)
        self.hide_btn = ttk.Checkbutton(controls, text="Hide days already imported",
                                        variable=self.hide_var, command=self._on_hide)
        self.hide_btn.pack(side="left", padx=(0, 14))
        self.merge_var = tk.BooleanVar(value=self.model.merge_all)
        self.merge_btn = ttk.Checkbutton(controls, text="One folder for all checked days",
                                         variable=self.merge_var, command=self._on_merge)
        self.merge_btn.pack(side="left")
        self.hidden_label = ttk.Label(controls, text="", foreground=MUTED_FG)
        self.hidden_label.pack(side="right")
        self._edit_widgets += [self.day_start_box, self.hide_btn, self.merge_btn]

        self.banner = ttk.Frame(box, padding=(0, 4))
        self.banner.grid(row=1, column=0, sticky="ew")
        self.banner_label = ttk.Label(self.banner, text="", foreground=WARN_FG,
                                      wraplength=700, justify="left")
        self.banner_label.pack(side="left", fill="x", expand=True)
        self.banner_btn = ttk.Button(self.banner, text="", command=self._on_banner)
        self.banner_btn.pack(side="right")
        self.banner.grid_remove()
        self._edit_widgets.append(self.banner_btn)

        self.days = ScrollableFrame(box)
        self.days.canvas.configure(height=150)
        self.days.grid(row=2, column=0, sticky="nsew", pady=(4, 4))
        self.days.on_scroll = self._schedule_build
        self.empty_label = ttk.Label(self.days.inner, text="", foreground=MUTED_FG)
        box.rowconfigure(2, weight=1)

        ttk.Label(box, text="Destination folders").grid(row=3, column=0, sticky="w")
        tree_frame = ttk.Frame(box)
        tree_frame.grid(row=4, column=0, sticky="ew")
        tree_frame.columnconfigure(0, weight=1)
        self.dest_tree = ttk.Treeview(tree_frame, columns=("status", "folder", "contents"),
                                      show="headings", height=3, selectmode="none")
        for cid, text, width in (("status", "", 80), ("folder", "Folder", 380),
                                 ("contents", "Contents", 360)):
            self.dest_tree.heading(cid, text=text, anchor="w")
            self.dest_tree.column(cid, width=width, minwidth=60, anchor="w",
                                  stretch=cid != "status")
        self.dest_tree.tag_configure("warn", foreground=WARN_FG)
        bar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.dest_tree.yview)
        self.dest_tree.configure(yscrollcommand=bar.set)
        self.dest_tree.grid(row=0, column=0, sticky="ew")
        bar.grid(row=0, column=1, sticky="ns")

        self.notices = ttk.Frame(box)
        self.notices.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        self.reimport_var = tk.BooleanVar(value=self.model.reimport_culled)
        self.problems_label = ttk.Label(box, text="", foreground=ERROR_FG, wraplength=800,
                                        justify="left")
        self.problems_label.grid(row=6, column=0, sticky="w")

    def _build_bottom(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.preview_btn = ttk.Button(bar, text="Preview...", command=self.open_preview)
        self.preview_btn.pack(side="left")
        self.settings_btn = ttk.Button(bar, text="Settings...", command=self.app.open_settings)
        self.settings_btn.pack(side="left", padx=6)
        self.import_btn = ttk.Button(bar, text="Import", command=self.start_import, width=22)
        self.import_btn.pack(side="right")
        self.space_label = ttk.Label(bar, text="")
        self.space_label.pack(side="right", padx=10)
        self.status_label = ttk.Label(bar, text="", foreground=MUTED_FG)
        self.status_label.pack(side="right", padx=10)

    # ---- sources ------------------------------------------------------------------------------

    def refresh_sources(self) -> None:
        f = self.sources_frame
        for child in f.winfo_children():
            child.destroy()
        self._status_labels.clear()
        self._source_vars.clear()
        self._source_widgets.clear()
        if not self.model.sources:
            ttk.Label(f, text="No memory card found yet. Insert a card, or add a folder.",
                      foreground=MUTED_FG).grid(row=0, column=0, sticky="w")
            return
        for r, e in enumerate(self.model.sources):
            scan = self.model.scans.get(e.key)
            var = tk.BooleanVar(value=e.checked)
            self._source_vars.append(var)
            cb = ttk.Checkbutton(f, variable=var,
                                 command=lambda k=e.key, v=var: self._on_source_check(k, v.get()))
            cb.grid(row=r, column=0, sticky="w")
            fg = MUTED_FG if not e.present else ""
            ttk.Label(f, text=e.title, font=self.bold, foreground=fg).grid(
                row=r, column=1, sticky="w", padx=(0, 10))
            ttk.Label(f, text=scan_summary(scan) if e.present else "", foreground=fg).grid(
                row=r, column=2, sticky="w", padx=(0, 10))
            status = ttk.Label(f, text=e.status_text(scan), wraplength=300, justify="left",
                               foreground=ERROR_FG if e.status == "error" else fg)
            status.grid(row=r, column=3, sticky="w", padx=(0, 10))
            self._status_labels[e.key] = status
            ttk.Label(f, text=short_path(e.source.root, 56), foreground=MUTED_FG).grid(
                row=r, column=4, sticky="w")
            self._source_widgets.append((cb, e.present))
            if not e.is_card or not e.present:
                remove = ttk.Button(f, text="Remove", width=8,
                                    command=lambda k=e.key: self._remove_source(k))
                remove.grid(row=r, column=5, sticky="e", padx=(6, 0))
                self._source_widgets.append((remove, True))
        for widget, on in self._source_widgets:
            set_state(widget, on and not self._locked)
        f.columnconfigure(4, weight=1)

    def on_scan_progress(self, source_key: str | None, ev: Progress) -> None:
        if source_key is None:
            return
        entry = self.model.find(source_key)
        if entry is None:
            return
        entry.detail = progress_text(ev)
        label = self._status_labels.get(source_key)
        if label is not None:
            label.configure(text=entry.status_text(None))

    def sources_changed(self) -> None:
        """Sources or their scans changed: redraw them, regroup the days and re-plan."""
        self.refresh_sources()
        self.regroup()

    def regroup(self) -> None:
        self.model.rebuild()
        self.refresh_days()
        self.schedule_plan()

    def _on_source_check(self, source_key: str, on: bool) -> None:
        self.model.set_source_checked(source_key, on)
        if not on:
            self.app.cancel_scan_of(source_key)
        self.sources_changed()
        self.app.queue_scans()

    def _on_add_folder(self) -> None:
        recent = self.app.settings.manual_sources
        path = filedialog.askdirectory(parent=self, title="Add a folder to import from",
                                       initialdir=recent[0] if recent else None, mustexist=True)
        if path:
            self.add_folder(Path(path))

    def add_folder(self, path: Path | str) -> None:
        entry = self.model.add_folder(path)
        if entry.status != "scanning":
            entry.status = "waiting"
        self.app.settings.remember_source(str(path))
        self.app.save_settings()
        self.sources_changed()
        self.app.queue_scans()

    def _remove_source(self, source_key: str) -> None:
        self.app.cancel_scan_of(source_key)
        self.model.remove_source(source_key)
        self.sources_changed()

    # ---- destination --------------------------------------------------------------------------

    def _on_browse(self) -> None:
        current = self.model.dest_path()
        path = filedialog.askdirectory(parent=self, title="Choose the destination folder",
                                       initialdir=str(current) if current else None,
                                       mustexist=True)
        if path:
            self.set_dest(str(Path(path)))

    def set_dest(self, text: str) -> None:
        self.dest_var.set(text)

    def _on_dest_changed(self, *_args: object) -> None:
        if self._syncing:
            return
        if self.model.set_dest(self.dest_var.get()):
            self.schedule_plan()
            self.dest_info.configure(text="")

    # ---- days ---------------------------------------------------------------------------------

    def refresh_days(self) -> None:
        rows = self.model.rows()
        cols = tuple(self.model.columns())
        if cols != self._grid_cols:
            self._reset_grid(cols)
        self._place_rows(rows)
        grid_key = (tuple(self._order), cols, bool(self.model.groups))
        if grid_key != self._grid_key:
            self._grid_key = grid_key
            self.days.scroll_top()
        self._build_visible()
        banner = self.model.banner()
        if banner:
            self.banner_label.configure(text=banner[0])
            self.banner_btn.configure(text=banner[1])
            self.banner.grid()
        else:
            self.banner.grid_remove()
        self.hidden_label.configure(text=self.model.hidden_status())
        self._sync_var(self.merge_var, self.model.merge_all)

    def _reset_grid(self, cols: tuple[str, ...]) -> None:
        """New count columns: every row is built again (the header too)."""
        inner = self.days.inner
        for w in self._pool.values():
            self._destroy_row(w)
        self._pool.clear()
        self._rows.clear()
        for lab in self._header:
            lab.destroy()
        for c in range(64):
            inner.columnconfigure(c, weight=0)
        headers = ["", "Day", "Shot", *cols, "New", "Same folder\nas above", "Event name"]
        self._header = [ttk.Label(inner, text=h, font=self.bold, justify="center")
                        for h in headers]
        self._name_col = len(headers) - 1
        inner.columnconfigure(self._name_col, weight=1)
        self._grid_cols = cols

    def _place_rows(self, rows: list[DayRow]) -> None:
        """Show the rows already built at their place; the others are built when seen."""
        known = {g.day for g in self.model.groups}
        for day in [d for d in self._pool if d not in known]:
            self._destroy_row(self._pool.pop(day))
        self._latest = {r.day: r for r in rows}
        self._order = [r.day for r in rows]
        for day, w in self._pool.items():
            if day not in self._latest and w.grid_row is not None:
                self._grid_row(w, None)
        for r, row in enumerate(rows, start=1):
            w = self._pool.get(row.day)
            if w is not None:
                self._update_row(w, row)
                if w.grid_row != r:
                    self._grid_row(w, r)
        self._rows = {d: self._pool[d] for d in self._order if d in self._pool}
        if rows:
            self.empty_label.grid_remove()
            for c, lab in enumerate(self._header):
                lab.grid(row=0, column=c, sticky="w", padx=4, pady=(0, 2))
        else:
            for lab in self._header:
                lab.grid_remove()
            text = ("Nothing new: every day is already imported. Untick \"Hide days already "
                    "imported\" to see them." if self.model.groups
                    else "No photos or videos yet. Insert a card or add a folder.")
            self.empty_label.configure(text=text)
            self.empty_label.grid(row=0, column=0, columnspan=8, sticky="w", padx=4, pady=8)
        self._size_rows()

    def _size_rows(self, *, force: bool = False) -> None:
        """Rows not built yet still take their height, so the scrollbar is right."""
        n = len(self._order)
        if not self._row_h or (n == self._sized_rows and not force):
            return
        inner = self.days.inner
        for r in range(1, max(n, self._sized_rows) + 1):
            inner.rowconfigure(r, minsize=self._row_h if r <= n else 0)
        self._sized_rows = n

    def _schedule_build(self) -> None:
        if self._build_after is None:
            self._build_after = self.after_idle(self._on_build_idle)

    def _on_build_idle(self) -> None:
        self._build_after = None
        self._build_visible()

    def _visible_range(self) -> range:
        n = len(self._order)
        if not self._row_h:
            return range(0)
        canvas = self.days.canvas
        first, _last = canvas.yview()
        total = self.days.inner.winfo_reqheight()
        top = first * total
        lo = max(0, int(top // self._row_h) - 1 - BUILD_MARGIN)
        hi = min(n, int((top + canvas.winfo_height()) // self._row_h) + BUILD_MARGIN)
        return range(lo, hi)

    def _build_visible(self) -> None:
        if self._grid_cols is None:
            return
        wanted = [*range(min(len(self._order), FIRST_ROWS)), *self._visible_range()]
        for i in wanted:
            day = self._order[i]
            if day in self._pool:
                continue
            w = self._make_row(self._latest[day], self._grid_cols)
            self._pool[day] = w
            self._rows[day] = w
            self._grid_row(w, i + 1)
            if not self._row_h:
                self._row_h = max(cell.winfo_reqheight() + 2 * int(opts.get("pady", 0))
                                  for cell, opts in w.cells)
                self._size_rows(force=True)

    def _make_row(self, row: DayRow, cols: tuple[str, ...]) -> _RowWidgets:
        inner = self.days.inner
        day = row.day
        check = tk.BooleanVar(value=row.checked)
        join = tk.BooleanVar(value=row.join)
        name = tk.StringVar(value=row.name)
        check_btn = ttk.Checkbutton(inner, variable=check,
                                    command=lambda d=day: self._on_day_check(d))
        labels = [ttk.Label(inner, text=row.label), ttk.Label(inner, text=row.shot)]
        cells: list[tuple[tk.Misc, dict[str, Any]]] = [
            (check_btn, {"column": 0, "padx": 4}),
            (labels[0], {"column": 1, "sticky": "w", "padx": 4}),
            (labels[1], {"column": 2, "sticky": "w", "padx": 4}),
        ]
        for c, folder in enumerate(cols, start=3):
            n = row.counts.get(folder, 0)
            lab = ttk.Label(inner, text=str(n) if n else "", anchor="e", width=5)
            cells.append((lab, {"column": c, "sticky": "e", "padx": 4}))
            labels.append(lab)
        new = ttk.Label(inner, text="", anchor="e", width=5, font=self.bold)
        join_btn = ttk.Checkbutton(inner, variable=join,
                                   command=lambda d=day: self._on_join(d))
        combo = ttk.Combobox(inner, textvariable=name, values=row.values, width=40)
        self.days.keep_wheel(combo)
        same = ttk.Label(inner, text=f"{UP} same folder as above", foreground=MUTED_FG)
        cells += [(new, {"column": 3 + len(cols), "sticky": "e", "padx": 4}),
                  (join_btn, {"column": 4 + len(cols), "padx": 4}),
                  (combo, {"column": self._name_col, "sticky": "ew", "padx": 4, "pady": 1}),
                  (same, {"column": self._name_col, "sticky": "w", "padx": 6})]
        name.trace_add("write", lambda *_a, d=day: self._on_name(d))
        combo.bind("<<ComboboxSelected>>", lambda _e, d=day: self._on_name(d))
        w = _RowWidgets(check, join, name, check_btn, join_btn, combo, same, new, labels,
                        cells=cells)
        self._update_row(w, row)
        return w

    def _grid_row(self, w: _RowWidgets, r: int | None) -> None:
        """Put the row's widgets on grid row ``r``, or take them off the grid (None)."""
        w.grid_row = r
        if r is None:
            for cell, _opts in w.cells:
                cell.grid_remove()
            return
        for cell, opts in w.cells:
            cell.grid(row=r, **opts)
        (w.combo if w.row is not None and w.row.join else w.same).grid_remove()

    def _destroy_row(self, w: _RowWidgets) -> None:
        for cell, _opts in w.cells:
            cell.destroy()

    def _update_row(self, w: _RowWidgets, row: DayRow) -> None:
        if w.row == row:
            return
        old, w.row = w.row, row
        was = self._syncing
        self._syncing = True
        try:
            self._sync_var(w.check, row.checked)
            self._sync_var(w.join, row.join)
            if w.name.get() != row.name:
                w.name.set(row.name)
            if old is None or old.values != row.values:
                w.combo.configure(values=row.values)
            w.new.configure(text="" if row.new is None else str(row.new))
            if old is not None and self._texts(old) != self._texts(row):
                for lab, text in zip(w.labels, self._texts(row), strict=False):
                    lab.configure(text=text)
            self._row_state(w, row)
            if old is None or old.checked != row.checked:
                fg = "" if row.checked else MUTED_FG
                for lab in (*w.labels, w.new):
                    lab.configure(foreground=fg)
            if w.grid_row is not None and (old is None or old.join != row.join):
                shown, hidden = (w.same, w.combo) if row.join else (w.combo, w.same)
                hidden.grid_remove()
                shown.grid()
        finally:
            self._syncing = was

    def _texts(self, row: DayRow) -> list[str]:
        counts = [str(row.counts.get(f, 0) or "") for f in self._grid_cols or ()]
        return [row.label, row.shot, *counts]

    def _row_state(self, w: _RowWidgets, row: DayRow) -> None:
        on = not self._locked
        set_state(w.check_btn, on)
        set_state(w.join_btn, on and row.join_enabled and row.checked)
        set_state(w.combo, on and row.checked)

    def _set_locked(self, locked: bool) -> None:
        """Disable every input while the final plan is built or an import/delete runs."""
        if locked == self._locked:
            return
        self._locked = locked
        for w in self._pool.values():
            if w.row is not None:
                self._row_state(w, w.row)
        for widget, on in self._source_widgets:
            set_state(widget, on and not locked)
        if self._reimport_btn is not None:
            set_state(self._reimport_btn, not locked)

    @staticmethod
    def _sync_var(var: tk.Variable, value: object) -> None:
        if var.get() != value:
            var.set(value)

    def _on_day_check(self, day: date) -> None:
        w = self._rows.get(day)
        if w is None:
            return
        self.model.set_checked(day, bool(w.check.get()))
        self.refresh_days()
        self.schedule_plan()

    def _on_join(self, day: date) -> None:
        w = self._rows.get(day)
        if w is None:
            return
        self.model.set_join(day, bool(w.join.get()))
        self.refresh_days()
        self.schedule_plan()

    def _on_name(self, day: date) -> None:
        if self._syncing:
            return
        w = self._rows.get(day)
        if w is None:
            return
        text = w.name.get()
        if self.model.names_by_date.get(day) == text:
            return
        self.model.set_name(day, text)
        self.schedule_plan()

    def set_name(self, day: date, text: str) -> None:
        """Type ``text`` into the event name box of ``day`` (as the user would)."""
        w = self._rows.get(day)
        if w is None:
            self.model.set_name(day, text)
            self.schedule_plan()
        else:
            w.name.set(text)

    def _on_day_start(self, _event: object = None) -> None:
        self.set_day_start(parse_hhmm(self.day_start_var.get(), self.model.day_start))

    def set_day_start(self, t: time) -> None:
        text = fmt_hhmm(t)
        if text not in self.day_start_box.cget("values"):
            self.day_start_box.configure(values=[*self.day_start_box.cget("values"), text])
        self._sync_var(self.day_start_var, text)
        if self.model.set_day_start(t):
            self.app.settings.day_start = text
            self.app.save_settings()
            self.refresh_days()
            self.schedule_plan()

    def _on_banner(self) -> None:
        t = self.model.banner_day_start()
        if t is not None:
            self.set_day_start(t)

    def _on_hide(self) -> None:
        self.set_hide_imported(bool(self.hide_var.get()))

    def set_hide_imported(self, on: bool) -> None:
        self._sync_var(self.hide_var, on)
        if on == self.model.hide_imported:
            return
        self.model.set_hide_imported(on)
        self.app.settings.hide_imported_days = on
        self.app.save_settings()
        self.refresh_days()
        self.schedule_plan()

    def _on_merge(self) -> None:
        self.model.set_merge_all(bool(self.merge_var.get()))
        self.refresh_days()
        self.schedule_plan()

    def _on_reimport(self) -> None:
        self.model.reimport_culled = bool(self.reimport_var.get())
        self.schedule_plan()

    def apply_general(self, g: GeneralSettings) -> None:
        """New defaults from the Settings dialog."""
        if g.dest_root and g.dest_root != self.dest_var.get():
            self.set_dest(g.dest_root)
        self.set_day_start(parse_hhmm(g.day_start, self.model.day_start))
        self.set_hide_imported(g.hide_imported)

    def sync_controls(self) -> None:
        """Make the checkboxes match the model again (after a New import reset)."""
        self._sync_var(self.merge_var, self.model.merge_all)
        self._sync_var(self.reimport_var, self.model.reimport_culled)
        self._sync_var(self.hide_var, self.model.hide_imported)
        self._grid_key = None
        self._interrupted = False

    # ---- live plan ----------------------------------------------------------------------------

    def schedule_plan(self, delay_ms: int = PLAN_DELAY_MS) -> None:
        """The inputs changed: plan again soon.

        A plan that is still running was computed from the old inputs, so it is cancelled
        and its result will be dropped (the generation moves on now, not when the timer
        fires). That includes the final plan of an Import click.
        """
        if not self.app.can_plan():
            return
        self._next_gen()
        if self.model.checking:
            self.model.checking = False
            self._interrupted = True
        self.model.plan_pending = True
        if self._plan_after is not None:
            self.after_cancel(self._plan_after)
        self._plan_after = self.after(delay_ms, self._start_plan)
        self.apply_state()

    def _cancel_timer(self) -> None:
        if self._plan_after is not None:
            self.after_cancel(self._plan_after)
            self._plan_after = None

    def stop(self) -> None:
        """The window is closing: no plan refresh may fire after the widgets are gone."""
        self._cancel_timer()
        self._plan_cancel.cancel()
        if self._build_after is not None:
            self.after_cancel(self._build_after)
            self._build_after = None

    def _next_gen(self) -> CancelToken:
        self.plan_gen += 1
        self._plan_cancel.cancel()
        self._plan_cancel = CancelToken()
        return self._plan_cancel

    def _start_plan(self) -> None:
        self._plan_after = None
        if not self.app.can_plan():
            return
        cancel = self._next_gen()
        req = self.model.plan_request(self.app.cfg)
        self.app.runner.run_light("plan", compute_plan, self.plan_gen, req, cancel=cancel)

    def on_plan_done(self, gen: int, result: PlanResult) -> None:
        if gen != self.plan_gen:
            return
        if not self.model.is_current(result):
            self.schedule_plan(0)
            return
        self.model.plan_pending = self._plan_after is not None
        changed = self.model.apply_plan(result)
        self._remember_dest()
        self.refresh_days()
        self.refresh_plan_views()
        if changed:
            self.schedule_plan(0)

    def _remember_dest(self) -> None:
        dest = self.model.dest_path()
        if self.model.dest_ok and dest is not None and self.app.settings.dest_root != str(dest):
            self.app.settings.dest_root = str(dest)
            self.app.save_settings()

    def on_plan_failed(self, gen: int, error: BaseException, details: str) -> None:
        if gen != self.plan_gen or isinstance(error, Cancelled):
            return
        log.error("planning failed: %s\n%s", error, details)
        self.model.checking = False
        self.model.plan_pending = self._plan_after is not None
        self.model.plan_failed(str(error) or type(error).__name__)
        self.refresh_plan_views()

    def refresh_plan_views(self) -> None:
        m = self.model
        self.dest_info.configure(text=m.dest_info())
        self.dest_tree.delete(*self.dest_tree.get_children())
        for r in m.destination_rows():
            self.dest_tree.insert("", "end", values=(r.status, r.folder, r.contents),
                                  tags=("warn",) if r.warning else ())
        self.dest_tree.configure(height=min(max(len(self.dest_tree.get_children()), 2), 5))
        for child in self.notices.winfo_children():
            child.destroy()
        self._reimport_btn = None
        for n in m.notices():
            row = ttk.Frame(self.notices)
            row.pack(anchor="w", fill="x")
            mark = f"{WARN} " if n.level == "warning" else "• "
            ttk.Label(row, text=mark + n.text, wraplength=760, justify="left",
                      foreground=WARN_FG if n.level == "warning" else "").pack(side="left")
            if n.action:
                ttk.Button(row, text="Details...",
                           command=lambda a=n.action: self._details(a)).pack(side="left", padx=6)
        if m.culled_seen or m.reimport_culled:
            n = m.culled_seen
            text = (f"Re-import {plural(n, 'removed file')}" if n
                    else "Re-import files removed from the library")
            self._reimport_btn = ttk.Checkbutton(self.notices, text=text,
                                                 variable=self.reimport_var,
                                                 command=self._on_reimport)
            self._reimport_btn.pack(anchor="w")
            set_state(self._reimport_btn, not self._locked)
        self.problems_label.configure(text="\n".join(m.problems()))
        self.space_label.configure(text=m.space_line())
        self.apply_state()

    def _details(self, action: str) -> None:
        if action == "unknown":
            TextDialog(self, "Unknown file types", self.model.unknown_details(),
                       intro="These files are not a photo or video type the app knows. They "
                             "stay on the cards. To import them, add their type in the config "
                             "file (Settings, File types).").show()
        else:
            TextDialog(self, "Could not read", self.model.error_details(),
                       intro="These files or folders could not be read from the source.").show()

    def apply_state(self) -> None:
        en = self.model.enabled(self.app.state)
        set_state(self.import_btn, en.import_)
        set_state(self.preview_btn, en.preview)
        set_state(self.settings_btn, en.settings)
        for w in self._edit_widgets:
            set_state(w, en.edit)
        self._set_locked(not en.edit)
        why = en.why_not
        if not why and self._interrupted:
            why = CHANGED_WHILE_CHECKING
        self.status_label.configure(text=why)
        self.import_btn.configure(text=self.model.import_label())

    # ---- preview and import -------------------------------------------------------------------

    def open_preview(self) -> None:
        plan = self.model.plan
        if plan is None:
            return
        en = self.model.enabled(self.app.state)
        PreviewDialog(self, "Preview", plan_rows(plan),
                      intro=f"What the import will do in {plan.dest_root}",
                      on_import=self.start_import if en.import_ else None).show()

    def start_import(self) -> None:
        """Re-plan with a write test of the destination, confirm, then import.

        The inputs stay disabled until the answer is in, so the plan that is imported is the
        one for what the window shows.
        """
        if not self.model.enabled(self.app.state).import_:
            return
        self._cancel_timer()
        cancel = self._next_gen()
        self._interrupted = False
        self.model.plan_pending = True
        self.model.checking = True
        self._final_inputs = self.model.inputs()
        self.apply_state()
        req = self.model.plan_request(self.app.cfg, probe_write=True)
        self.app.runner.run_light("final", compute_plan, self.plan_gen, req, cancel=cancel)

    def on_final_plan(self, gen: int, result: PlanResult) -> None:
        if gen != self.plan_gen:
            return
        if self.model.inputs() != self._final_inputs or not self.model.is_current(result):
            self.schedule_plan(0)              # stale: never import it (says so on the page)
            return
        self.model.plan_pending = False
        changed = self.model.apply_plan(result)
        self.refresh_days()
        self.refresh_plan_views()
        if changed:
            self.schedule_plan(0)
            return
        plan = result.plan
        go = plan is not None and not plan.problems and bool(plan.to_copy)
        if go and self.app.settings.ask_before_import:
            go = self.app.confirm("Import", confirm_text(plan))
            if gen != self.plan_gen:           # a card came or went while the question was up
                return
        self.model.checking = False
        if go and plan is not None:
            self.app.begin_import(plan, self.model.names_to_remember())
        self.apply_state()
