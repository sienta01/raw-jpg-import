"""Modal dialogs: preview, settings, delete-from-cards, errors and plain text details.

Each dialog is a ``Toplevel`` made transient to the main window with a grab while shown.
Building a dialog never blocks; :meth:`_Modal.show` waits until it is closed.
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .. import __version__, appdirs
from ..config import Config, write_template
from ..deleter import DeletionPlan
from ..report import fmt_bytes, plural
from .viewmodel import DAY_STARTS, PreviewRow, deletion_rows
from .widgets import ERROR_FG, MUTED_FG, WARN_FG, open_path, open_text_file, set_state

log = logging.getLogger(__name__)

BATCH = 500
ALL = "All"
DOT_SEP = "   \u00b7   "


class _Modal(tk.Toplevel):
    """``size`` is the size at 100 % display scaling ("760x420").

    It grows with the scaling and is never smaller than what the content asks for (within
    the screen). Subclasses pack their button row first, at the bottom, so a dialog that is
    still too small shrinks its list or text box instead of hiding the buttons.
    """

    def __init__(self, parent: tk.Misc, title: str, *, size: str | None = None) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self._size = size
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)

    def present(self) -> bool:
        """Show the dialog over its parent and take the grab.

        False when the parent window is withdrawn (tests, shutting down): nobody could
        answer, so the dialog stays hidden.
        """
        parent = self.master.winfo_toplevel() if self.master else None
        if parent is None:
            return False
        try:
            state = parent.wm_state()
            if state == "withdrawn":
                return False
            if state == "iconic":
                parent.deiconify()
        except tk.TclError:
            return False
        self.update_idletasks()
        w, h = self.fit_size()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 3)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        if 0 <= parent.winfo_rootx() < screen_w and 0 <= parent.winfo_rooty() < screen_h:
            x = max(0, min(x, screen_w - w))       # wider than the window: stay on screen
            y = max(0, min(y, screen_h - h - 40))
        self.geometry(f"{w}x{h}+{x}+{y}" if self._size else f"+{x}+{y}")
        self.deiconify()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        return True

    def fit_size(self) -> tuple[int, int]:
        """The size to open at: the scaled ``size``, at least the requested size, on screen."""
        req_w, req_h = self.winfo_reqwidth(), self.winfo_reqheight()
        if not self._size:
            return req_w, req_h
        base_w, base_h = (int(v) for v in self._size.split("x"))
        try:
            f = max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72))
        except (tk.TclError, ValueError):
            f = 1.0
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight() - 80
        w = min(max(int(base_w * f), req_w), screen_w)
        h = min(max(int(base_h * f), req_h), screen_h)
        self.minsize(min(int(360 * f), w), min(int(220 * f), h))
        return w, h

    def show(self) -> None:
        """Wait until the dialog is closed (returns at once if it cannot be shown)."""
        if not self.present():
            log.warning("dialog %r not shown: its window is hidden", self.title())
            self.close()
            return
        self.wait_window(self)

    def close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def _tree(master: tk.Misc, columns: Sequence[tuple[str, str, int]], *,
          height: int = 12) -> ttk.Treeview:
    """A Treeview with scrollbars inside ``master`` (grid row 0, column 0)."""
    frame = ttk.Frame(master)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height)
    for cid, text, width in columns:
        tree.heading(cid, text=text, anchor="w")
        tree.column(cid, width=width, minwidth=40, stretch=True, anchor="w")
    vbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")
    hbar.grid(row=1, column=0, sticky="ew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    tree.frame = frame  # type: ignore[attr-defined]
    return tree


# ---- preview ---------------------------------------------------------------------------------


class PreviewDialog(_Modal):
    """Every planned action (or import result), filterable, inserted in batches."""

    COLUMNS = (("action", "Action", 130), ("source", "Source", 260),
               ("dest", "→ Destination", 320), ("taken", "Taken", 170),
               ("size", "Size", 80), ("note", "Note", 260))

    def __init__(self, parent: tk.Misc, title: str, rows: Sequence[PreviewRow], *,
                 intro: str = "", on_import: Callable[[], None] | None = None) -> None:
        super().__init__(parent, title, size="1100x560")
        self.rows = list(rows)
        self._after: str | None = None
        self._pending: list[PreviewRow] = []
        counts = Counter(r.action for r in self.rows)
        chips = DOT_SEP.join(f"{a}: {n}" for a, n in counts.most_common()) or "Nothing to show"
        if intro:
            ttk.Label(self.body, text=intro, wraplength=1000, justify="left").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(self.body, text=chips, foreground=MUTED_FG, wraplength=1000,
                  justify="left").grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(self.body, text="Show:").grid(row=2, column=0, sticky="w", pady=6)
        self.filter = tk.StringVar(value=ALL)
        combo = ttk.Combobox(self.body, textvariable=self.filter, state="readonly", width=30,
                             values=[ALL] + [a for a, _ in counts.most_common()])
        combo.grid(row=2, column=1, sticky="w", pady=6)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.refill())
        self.shown = ttk.Label(self.body, text="", foreground=MUTED_FG)
        self.shown.grid(row=2, column=2, sticky="e")
        self.tree = _tree(self.body, self.COLUMNS, height=16)
        self.tree.frame.grid(row=3, column=0, columnspan=3, sticky="nsew")  # type: ignore
        buttons = ttk.Frame(self.body)
        buttons.grid(row=4, column=0, columnspan=3, sticky="e", pady=(10, 0))
        if on_import is not None:
            def do_import() -> None:
                self.close()
                on_import()
            ttk.Button(buttons, text="Import...", command=do_import).pack(side="left", padx=4)
        ttk.Button(buttons, text="Close", command=self.close).pack(side="left", padx=4)
        self.body.columnconfigure(2, weight=1)
        self.body.rowconfigure(3, weight=1)
        self.refill()

    def refill(self) -> None:
        if self._after is not None:
            self.after_cancel(self._after)
            self._after = None
        self.tree.delete(*self.tree.get_children())
        want = self.filter.get()
        self._pending = [r for r in self.rows if want == ALL or r.action == want]
        self.shown.configure(text=plural(len(self._pending), "file"))
        self._pending.reverse()
        self._insert_batch()

    def _insert_batch(self) -> None:
        self._after = None
        for _ in range(min(BATCH, len(self._pending))):
            r = self._pending.pop()
            self.tree.insert("", "end", values=(r.action, r.source, r.dest, r.taken, r.size,
                                                r.note))
        if self._pending:
            self._after = self.after(1, self._insert_batch)

    def close(self) -> None:
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        super().close()


# ---- text and errors -------------------------------------------------------------------------


class TextDialog(_Modal):
    def __init__(self, parent: tk.Misc, title: str, text: str, *, intro: str = "") -> None:
        super().__init__(parent, title, size="760x420")
        if intro:
            ttk.Label(self.body, text=intro, wraplength=720, justify="left").pack(
                anchor="w", pady=(0, 6))
        ttk.Button(self.body, text="Close", command=self.close).pack(
            side="bottom", anchor="e", pady=(10, 0))
        box = ScrolledText(self.body, height=16, wrap="none")
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)


class ErrorDialog(_Modal):
    def __init__(self, parent: tk.Misc, title: str, message: str, details: str = "") -> None:
        super().__init__(parent, title, size="720x400" if details else None)
        self.details = details
        ttk.Label(self.body, text=message, wraplength=680, justify="left",
                  foreground=ERROR_FG).pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(self.body)
        buttons.pack(side="bottom", anchor="e", pady=(10, 0))
        if details:
            ttk.Button(buttons, text="Copy details", command=self.copy).pack(side="left", padx=4)
        close = ttk.Button(buttons, text="Close", command=self.close)
        close.pack(side="left", padx=4)
        close.focus_set()
        if details:
            box = ScrolledText(self.body, height=12, wrap="none")
            box.insert("1.0", details)
            box.configure(state="disabled")
            box.pack(fill="both", expand=True)

    def copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.details)


def show_error(parent: tk.Misc, title: str, message: str, details: str = "") -> None:
    ErrorDialog(parent, title, message, details).show()


def confirm(parent: tk.Misc, title: str, message: str) -> bool:
    return bool(messagebox.askyesno(title, message, parent=parent))


# ---- delete from cards -----------------------------------------------------------------------


class DeleteDialog(_Modal):
    """Asks before deleting verified files from the cards; Delete stays off until ticked."""

    def __init__(self, parent: tk.Misc, dplan: DeletionPlan) -> None:
        super().__init__(parent, "Delete imported files from cards", size="760x520")
        self.result = False
        per, excluded = deletion_rows(dplan)
        n = len(dplan.candidates)
        ttk.Label(self.body, text=f"Delete {plural(n, 'file')} ({fmt_bytes(dplan.bytes)}) "
                                  "from the cards?", font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w")
        ttk.Label(self.body, wraplength=720, justify="left",
                  text="Only files whose copy in your library is verified are deleted. Each "
                       "one is compared with its copy again right before it is removed. "
                       "Folders and camera files are never touched. To look at the copies "
                       "first, cancel and click View photos.").pack(anchor="w", pady=(4, 8))
        for warning in dplan.warnings:
            ttk.Label(self.body, text=f"⚠ {warning}", foreground=WARN_FG, wraplength=720,
                      justify="left").pack(anchor="w", pady=(0, 8))
        cards = ttk.Frame(self.body)
        cards.pack(anchor="w", fill="x")
        for row, (label, files, size) in enumerate(per):
            ttk.Label(cards, text=f"{label}:").grid(row=row, column=0, sticky="w", padx=(0, 12))
            ttk.Label(cards, text=f"{files}  ({size})").grid(row=row, column=1, sticky="w")
        # bottom up: the buttons, the confirmation and the tip; the kept list takes the rest
        buttons = ttk.Frame(self.body)
        buttons.pack(side="bottom", anchor="e")
        self.delete_button = ttk.Button(buttons, text=f"Delete {plural(n, 'file')}",
                                        command=self._delete)
        self.delete_button.pack(side="left", padx=4)
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.close)
        self.cancel_button.pack(side="left", padx=4)
        self.understood = tk.BooleanVar(value=False)
        self.understood_box = ttk.Checkbutton(
            self.body, variable=self.understood, command=self._sync,
            text="I understand these files will be permanently removed from the cards")
        self.understood_box.pack(side="bottom", anchor="w", pady=(4, 8))
        ttk.Label(self.body, foreground=MUTED_FG, wraplength=720, justify="left",
                  text="Tip: formatting the card in the camera is faster and keeps the card "
                       "in good shape.").pack(side="bottom", anchor="w", pady=(10, 4))
        if excluded:
            ttk.Label(self.body, text=f"Kept on the cards ({len(excluded)}):").pack(
                anchor="w", pady=(10, 2))
            tree = _tree(self.body, (("file", "File", 300), ("reason", "Why", 380)), height=6)
            for f, reason in excluded:
                tree.insert("", "end", values=(f, reason))
            tree.frame.pack(fill="both", expand=True)  # type: ignore[attr-defined]
        self.cancel_button.focus_set()
        self._sync()

    def _sync(self) -> None:
        set_state(self.delete_button, self.understood.get())

    def _delete(self) -> None:
        if not self.understood.get():
            return
        self.result = True
        self.close()

    def ask(self) -> bool:
        self.show()
        return self.result


# ---- settings --------------------------------------------------------------------------------


@dataclass
class GeneralSettings:
    dest_root: str
    day_start: str
    hide_imported: bool
    ask_before_import: bool


class SettingsDialog(_Modal):
    """General defaults, the effective file types (with the config file), and About."""

    def __init__(self, parent: tk.Misc, general: GeneralSettings, cfg: Config,
                 cfg_error: str | None, *,
                 on_apply: Callable[[GeneralSettings], None],
                 on_reload: Callable[[], tuple[Config, str | None]]) -> None:
        super().__init__(parent, "Settings", size="760x500")
        self.on_apply = on_apply
        self.on_reload = on_reload
        buttons = ttk.Frame(self.body)
        buttons.pack(side="bottom", anchor="e", pady=(10, 0))
        self.ok_button = ttk.Button(buttons, text="OK", command=self._ok)
        self.ok_button.pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="left", padx=4)
        nb = ttk.Notebook(self.body)
        nb.pack(fill="both", expand=True)
        nb.add(self._general(nb, general), text="General")
        self.types = ttk.Frame(nb, padding=10)
        nb.add(self.types, text="File types")
        nb.add(self._about(nb), text="About")
        self._fill_types(cfg, cfg_error)

    def _general(self, nb: ttk.Notebook, g: GeneralSettings) -> ttk.Frame:
        f = ttk.Frame(nb, padding=10)
        self.dest = tk.StringVar(value=g.dest_root)
        self.day_start = tk.StringVar(value=g.day_start)
        self.hide = tk.BooleanVar(value=g.hide_imported)
        self.ask = tk.BooleanVar(value=g.ask_before_import)
        ttk.Label(f, text="Default destination:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.dest, width=50).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="Browse...", command=self._browse).grid(row=0, column=2, padx=4)
        ttk.Label(f, text="A new day starts at:").grid(row=1, column=0, sticky="w", pady=4)
        values = list(DAY_STARTS)
        if g.day_start not in values:
            values.append(g.day_start)
        ttk.Combobox(f, textvariable=self.day_start, values=values, state="readonly",
                     width=8).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(f, foreground=MUTED_FG, wraplength=600, justify="left",
                  text="Photos taken after midnight but before this time count as the "
                       "previous day.").grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Checkbutton(f, text="Hide days already imported", variable=self.hide).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Checkbutton(f, text="Ask before importing", variable=self.ask).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=4)
        f.columnconfigure(1, weight=1)
        return f

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self, title="Default destination",
                                       initialdir=self.dest.get() or None, mustexist=True)
        if path:
            self.dest.set(str(Path(path)))

    def _fill_types(self, cfg: Config, cfg_error: str | None) -> None:
        for child in self.types.winfo_children():
            child.destroy()
        f = self.types
        if cfg_error:
            ttk.Label(f, text=f"Your config file has an error, so importing is off until it "
                              f"is fixed:\n{cfg_error}", foreground=ERROR_FG, wraplength=680,
                      justify="left").pack(anchor="w", pady=(0, 6))
        tree = _tree(f, (("type", "Type", 90), ("folder", "Folder", 140),
                         ("exts", "Extensions", 420)), height=9)
        t = cfg.formats
        for c in sorted(t.categories, key=lambda c: (c.role != "primary", c.rank, c.key)):
            where = c.folder or {"sidecar": "(with its photo)", "ignore": "(ignored)"}.get(
                c.role, "")
            tree.insert("", "end", values=(c.key, where, " ".join(sorted(c.extensions))))
        tree.frame.pack(fill="both", expand=True)  # type: ignore[attr-defined]
        policy = (f"copied into {t.other_folder}" if t.unknown_policy == "copy"
                  else "reported, not imported")
        ttk.Label(f, text=f"Unknown file types are {policy}.").pack(anchor="w", pady=(6, 0))
        path = appdirs.config_file()
        state = "" if path.is_file() else "  (not created yet: built-in defaults are used)"
        ttk.Label(f, text=f"Config file: {path}{state}", foreground=MUTED_FG,
                  wraplength=680, justify="left").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(6, 0))
        ttk.Button(row, text="Open config file", command=self._open_config).pack(
            side="left", padx=(0, 6))
        ttk.Button(row, text="Reload", command=self._reload).pack(side="left")

    def _open_config(self) -> None:
        path = appdirs.config_file()
        try:
            if not path.is_file():
                write_template(path)
            open_text_file(path)
        except OSError as e:
            show_error(self, "Config file", f"Could not open {path}: {e}")
        self._fill_types(*self.on_reload())

    def _reload(self) -> None:
        self._fill_types(*self.on_reload())

    def _about(self, nb: ttk.Notebook) -> ttk.Frame:
        f = ttk.Frame(nb, padding=10)
        ttk.Label(f, text=f"Raw/JPG Import {__version__}",
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=680, justify="left",
                  text="Copies photos and videos from camera cards into dated event folders "
                       "split into RAW, JPG and MOV. Every copy is checked against the card. "
                       "Nothing on the cards is changed unless you ask to delete imported "
                       "files.").pack(anchor="w", pady=(6, 10))
        logs = appdirs.log_dir()
        ttk.Label(f, text=f"Logs: {logs}", foreground=MUTED_FG, wraplength=680,
                  justify="left").pack(anchor="w")
        ttk.Button(f, text="Open log folder", command=self._open_logs).pack(anchor="w",
                                                                            pady=(6, 0))
        return f

    def _open_logs(self) -> None:
        logs = appdirs.log_dir()
        try:
            logs.mkdir(parents=True, exist_ok=True)
            open_path(logs)
        except OSError as e:
            show_error(self, "Logs", f"Could not open {logs}: {e}")

    def values(self) -> GeneralSettings:
        return GeneralSettings(dest_root=self.dest.get().strip(),
                               day_start=self.day_start.get(), hide_imported=self.hide.get(),
                               ask_before_import=self.ask.get())

    def _ok(self) -> None:
        self.on_apply(self.values())
        self.close()
