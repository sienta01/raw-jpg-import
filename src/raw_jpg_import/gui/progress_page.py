"""The page shown while importing or deleting: progress, counters, a log and Cancel."""

from __future__ import annotations

import tkinter as tk
from tkinter import font, ttk
from tkinter.scrolledtext import ScrolledText
from typing import TYPE_CHECKING

from ..copier import ImportReport
from ..deleter import DeletionPlan
from ..events import FileDone, Notice, Progress
from ..planner import Action, ImportPlan
from ..report import fmt_bytes, plural
from .viewmodel import progress_fraction, progress_text, short_path
from .widgets import MUTED_FG, set_state

if TYPE_CHECKING:
    from .app import App

MAX_LOG_LINES = 500
BAR_MAX = 1000

CANCEL_IMPORT = "Stop importing? Files already copied stay. Nothing on the cards is touched."
CANCEL_DELETE = "Stop deleting? Files already deleted are gone; the rest stay on the cards."

_SKIPPED = (Action.SKIP_IMPORTED, Action.SKIP_EXISTS, Action.SKIP_DUPLICATE, Action.SKIP_CULLED)


class ProgressPage(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=24)
        self.app = app
        self.phase = "import"
        self.plan: ImportPlan | None = None
        self.counts: dict[str, int] = {}
        big = font.nametofont("TkDefaultFont").copy()
        big.configure(size=max(12, int(big.cget("size")) + 4), weight="bold")
        self.columnconfigure(0, weight=1)
        self.title = ttk.Label(self, text="Importing...", font=big)
        self.title.grid(row=0, column=0, sticky="w")
        self.subtitle = ttk.Label(self, text="", foreground=MUTED_FG, wraplength=800,
                                  justify="left")
        self.subtitle.grid(row=1, column=0, sticky="w", pady=(2, 12))
        self.bar = ttk.Progressbar(self, orient="horizontal", mode="determinate",
                                   maximum=BAR_MAX)
        self.bar.grid(row=2, column=0, sticky="ew")
        self.files_label = ttk.Label(self, text="")
        self.files_label.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.current_label = ttk.Label(self, text="", foreground=MUTED_FG)
        self.current_label.grid(row=4, column=0, sticky="w", pady=(2, 10))
        self.counter_frame = ttk.Frame(self)
        self.counter_frame.grid(row=5, column=0, sticky="w")
        self.counter_labels: dict[str, ttk.Label] = {}
        self.details_btn = ttk.Button(self, text="Show details", command=self.toggle_log)
        self.details_btn.grid(row=6, column=0, sticky="w", pady=(14, 4))
        self.log_box = ScrolledText(self, height=14, wrap="none", state="disabled")
        self.log_box.grid(row=7, column=0, sticky="nsew")
        self.log_box.grid_remove()
        self.rowconfigure(7, weight=1)
        self.cancel_btn = ttk.Button(self, text="Cancel", command=self.cancel)
        self.cancel_btn.grid(row=8, column=0, sticky="e", pady=(12, 0))
        self._log_lines = 0
        self._log_shown = False

    # ---- starting -----------------------------------------------------------------------------

    def _reset(self, phase: str, title: str, subtitle: str, counters: list[str]) -> None:
        self.phase = phase
        self.title.configure(text=title)
        self.subtitle.configure(text=subtitle)
        self.bar.configure(value=0)
        self.files_label.configure(text="")
        self.current_label.configure(text="")
        for child in self.counter_frame.winfo_children():
            child.destroy()
        self.counter_labels.clear()
        self.counts = dict.fromkeys(counters, 0)
        for c, name in enumerate(counters):
            ttk.Label(self.counter_frame, text=f"{name}:").grid(row=0, column=2 * c, padx=(0, 4))
            lab = ttk.Label(self.counter_frame, text="0", width=6)
            lab.grid(row=0, column=2 * c + 1, padx=(0, 18), sticky="w")
            self.counter_labels[name] = lab
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._log_lines = 0
        self.cancel_btn.configure(text="Cancel")
        set_state(self.cancel_btn, True)

    def begin_import(self, plan: ImportPlan) -> None:
        self.plan = plan
        copies = plan.to_copy
        self._reset("import", "Importing...",
                    f"Copying {plural(len(copies), 'file')} ({fmt_bytes(plan.bytes_to_copy)}) "
                    f"into {short_path(plan.dest_root, 70)}. "
                    "Every copy is checked against the card.",
                    ["Copied", "Skipped", "Renamed", "Failed"])
        skipped = sum(1 for i in plan.items if i.action in _SKIPPED)
        self._bump("Skipped", skipped)
        self.files_label.configure(text=f"0 / {plural(len(copies), 'file')}")
        self.log(f"Import into {plan.dest_root}: {plural(len(copies), 'file')} to copy, "
                 f"{skipped} skipped")

    def begin_delete(self, dplan: DeletionPlan, report: ImportReport | None) -> None:
        self.plan = report.plan if report is not None else None
        n = len(dplan.candidates)
        self._reset("delete", "Deleting imported files from the cards...",
                    f"Checking each of {plural(n, 'file')} against its copy, then deleting "
                    "it from the card.", ["Deleted", "Kept", "Failed"])
        self.files_label.configure(text=f"0 / {plural(n, 'file')} checked")

    # ---- events -------------------------------------------------------------------------------

    def _bump(self, name: str, n: int = 1) -> None:
        if name not in self.counts:
            return
        self.counts[name] += n
        self.counter_labels[name].configure(text=str(self.counts[name]))

    def on_progress(self, ev: Progress) -> None:
        self.bar.configure(value=int(progress_fraction(ev) * BAR_MAX))
        self.files_label.configure(text=progress_text(ev))
        if ev.current:
            self.current_label.configure(text=ev.current)

    def _item_name(self, item_id: int) -> str:
        if self.plan is not None and 0 <= item_id < len(self.plan.items):
            item = self.plan.items[item_id]
            return f"{item.src.source.label}: {item.src.rel}"
        return f"file {item_id}"

    def on_file_done(self, ev: FileDone) -> None:
        name = self._item_name(ev.item_id)
        if self.phase == "delete":
            label = {"DELETED": "Deleted", "KEPT": "Kept", "FAILED": "Failed"}.get(ev.outcome)
            if label:
                self._bump(label)
            if ev.outcome != "DELETED":
                self.log(f"{ev.outcome.lower()}: {name}" + (f" ({ev.message})"
                                                            if ev.message else ""))
            return
        if ev.outcome == "COPIED":
            self._bump("Copied")
            item = self.plan.items[ev.item_id] if self.plan is not None else None
            if item is not None and item.renamed:
                self._bump("Renamed")
            dest = f" -> {item.target.folder}/{item.dest_rel}" if item and item.target else ""
            self.log(f"copied {name}{dest}")
        elif ev.outcome == "FAILED":
            self._bump("Failed")
            self.log(f"FAILED {name}: {ev.message}")
        elif ev.outcome in ("ALREADY_THERE", "DUPLICATE"):
            self._bump("Skipped")
            self.log(f"already there: {name}")

    def on_notice(self, ev: Notice) -> None:
        self.log(f"{ev.level}: {ev.text}")

    def log(self, line: str) -> None:
        box = self.log_box
        box.configure(state="normal")
        box.insert("end", line + "\n")
        self._log_lines += 1
        if self._log_lines > MAX_LOG_LINES:
            extra = self._log_lines - MAX_LOG_LINES
            box.delete("1.0", f"{extra + 1}.0")
            self._log_lines = MAX_LOG_LINES
        box.configure(state="disabled")
        box.see("end")

    def toggle_log(self) -> None:
        self._log_shown = not self._log_shown
        if self._log_shown:
            self.log_box.grid()
            self.details_btn.configure(text="Hide details")
        else:
            self.log_box.grid_remove()
            self.details_btn.configure(text="Show details")

    # ---- cancelling ---------------------------------------------------------------------------

    def cancel_question(self) -> str:
        return CANCEL_DELETE if self.phase == "delete" else CANCEL_IMPORT

    def cancel(self) -> None:
        if not self.app.confirm("Cancel", self.cancel_question()):
            return
        self.set_cancelling()
        self.app.cancel_job()

    def set_cancelling(self) -> None:
        self.cancel_btn.configure(text="Cancelling...")
        set_state(self.cancel_btn, False)
