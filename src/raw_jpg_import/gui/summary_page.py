"""The page shown after an import: results, folders, and what to do next."""

from __future__ import annotations

import tkinter as tk
from tkinter import font, ttk
from typing import TYPE_CHECKING

from ..copier import ImportReport
from ..deleter import DeletionReport
from ..report import deletion_summary_lines, plural
from .dialogs import PreviewDialog, show_error
from .viewmodel import (
    RESULT_GROUPS,
    can_delete,
    result_rows,
    short_path,
    summary_rows,
    summary_status,
)
from .widgets import MUTED_FG, ScrollableFrame, label_fg, open_path, set_state

if TYPE_CHECKING:
    from .app import App

MAX_FOLDERS = 100
MAX_DELETION_LINES = 200


class SummaryPage(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=24)
        self.app = app
        self.report: ImportReport | None = None
        self.deleted = False
        big = font.nametofont("TkDefaultFont").copy()
        big.configure(size=max(12, int(big.cget("size")) + 4), weight="bold")
        self.bold = font.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.columnconfigure(0, weight=1)
        self.status = ttk.Label(self, text="", font=big, wraplength=820, justify="left")
        self.status.grid(row=0, column=0, sticky="w")
        self.when = ttk.Label(self, text="", foreground=MUTED_FG)
        self.when.grid(row=1, column=0, sticky="w", pady=(2, 12))
        # Everything whose length depends on the import scrolls; the buttons always show.
        self.content = ScrollableFrame(self)
        self.content.canvas.configure(height=200)
        self.content.grid(row=2, column=0, sticky="nsew")
        self.rowconfigure(2, weight=1)
        inner = self.content.inner
        inner.columnconfigure(0, weight=1)
        self.deletion = ttk.Label(inner, text="", wraplength=820, justify="left")
        self.deletion.grid(row=0, column=0, sticky="w", pady=(0, 14))
        self.deletion.grid_remove()             # shown at the top once files were deleted
        self.rows = ttk.Frame(inner)
        self.rows.grid(row=1, column=0, sticky="w")
        ttk.Label(inner, text="Folders").grid(row=2, column=0, sticky="w", pady=(14, 2))
        self.folders = ttk.Frame(inner)
        self.folders.grid(row=3, column=0, sticky="ew")
        self.bar = bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(bar, text="Run again", command=self.app.run_again).pack(side="left")
        self.view_btn = ttk.Button(bar, text="View photos", command=self.view_all)
        self.view_btn.pack(side="left", padx=6)
        self.log_btn = ttk.Button(bar, text="Open log", command=self.open_log)
        self.log_btn.pack(side="left", padx=6)
        self.delete_btn = ttk.Button(bar, text="Delete imported files from cards...",
                                     command=self.app.ask_delete)
        self.delete_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="New import", command=self.app.new_import).pack(side="right")

    def show_report(self, report: ImportReport) -> None:
        self.report = report
        self.deleted = False
        text, level = summary_status(report)
        self.status.configure(text=text, foreground=label_fg(level))
        secs = int((report.finished - report.started).total_seconds())
        self.when.configure(text=f"Finished at {report.finished:%H:%M}, took {secs // 60} min "
                                 f"{secs % 60:02d} s")
        for child in self.rows.winfo_children():
            child.destroy()
        for r, row in enumerate(summary_rows(report)):
            fg = label_fg("error") if row.group == "failed" else ""
            ttk.Label(self.rows, text=f"{row.label}:", foreground=fg).grid(
                row=r, column=0, sticky="w", padx=(0, 10))
            ttk.Label(self.rows, text=str(row.count), foreground=fg, width=7, anchor="e").grid(
                row=r, column=1, sticky="e")
            ttk.Label(self.rows, text=row.extra, foreground=MUTED_FG).grid(
                row=r, column=2, sticky="w", padx=(8, 8))
            if row.count:
                ttk.Button(self.rows, text="Details",
                           command=lambda g=row.group: self.details(g)).grid(
                    row=r, column=3, sticky="w", pady=1)
        for child in self.folders.winfo_children():
            child.destroy()
        if not report.folders:
            ttk.Label(self.folders, text="No folders were filled.", foreground=MUTED_FG).grid(
                row=0, column=0, sticky="w")
        for r, folder in enumerate(report.folders[:MAX_FOLDERS]):
            ttk.Button(self.folders, text="View", width=8,
                       command=lambda p=folder: self.view(p)).grid(row=r, column=0, pady=1,
                                                                   padx=(0, 4))
            ttk.Button(self.folders, text="Open", width=8,
                       command=lambda p=folder: self._open(p)).grid(row=r, column=1, pady=1,
                                                                    padx=(0, 8))
            ttk.Label(self.folders, text=folder.name, font=self.bold).grid(row=r, column=2,
                                                                          sticky="w")
            ttk.Label(self.folders, text=short_path(folder.parent, 70),
                      foreground=MUTED_FG).grid(row=r, column=3, sticky="w", padx=(10, 0))
        more = len(report.folders) - MAX_FOLDERS
        if more > 0:
            ttk.Label(self.folders, text=f"... and {plural(more, 'more folder')}",
                      foreground=MUTED_FG).grid(row=MAX_FOLDERS, column=2, sticky="w")
        self.deletion.configure(text="")
        self.deletion.grid_remove()
        self.content.scroll_top()
        self.refresh_buttons()

    def show_deletion(self, dreport: DeletionReport) -> None:
        self.deleted = True
        lines = deletion_summary_lines(dreport)
        if dreport.status == "cancelled":
            lines.insert(0, "Deleting was cancelled; the remaining files stay on the cards.")
        if len(lines) > MAX_DELETION_LINES:
            more = len(lines) - MAX_DELETION_LINES
            lines = lines[:MAX_DELETION_LINES] + [f"  ... and {more} more (see the log)"]
        self.deletion.configure(text="\n".join(lines))
        self.deletion.grid()
        self.content.scroll_top()
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        set_state(self.view_btn, self.report is not None and bool(self.report.folders))
        set_state(self.log_btn, self.report is not None and self.report.log_path is not None)
        set_state(self.delete_btn, can_delete(self.report) and not self.deleted)

    def details(self, group: str) -> None:
        if self.report is None:
            return
        PreviewDialog(self, RESULT_GROUPS[group][0], result_rows(self.report, group)).show()

    def view(self, folder: object) -> None:
        if self.report is not None:
            self.app.open_viewer(list(self.report.folders), folder)  # type: ignore[arg-type]

    def view_all(self) -> None:
        if self.report is not None and self.report.folders:
            self.app.open_viewer(list(self.report.folders))

    def open_log(self) -> None:
        if self.report is not None and self.report.log_path is not None:
            self._open(self.report.log_path)

    def _open(self, path: object) -> None:
        try:
            open_path(path)  # type: ignore[arg-type]
        except OSError as e:
            show_error(self, "Open", f"Could not open {path}: {e}")
