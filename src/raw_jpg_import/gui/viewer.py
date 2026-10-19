"""The built-in photo viewer: check what an import put into an event folder.

A thumbnail grid shows one tile per shot (NEF + JPG together, with badges), with filters
that make a missing partner easy to spot. Clicking a tile shows it large; arrow keys move
through the shots. Previews are decoded on worker threads (see :mod:`previews`); only the
Tk thread touches widgets and PhotoImages. The viewer never changes any file.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from tkinter import font, ttk
from typing import Any

from .. import previews
from ..formats import FormatTable
from ..previews import LibraryFile, LibraryShot, Preview
from ..report import fmt_bytes, plural
from .widgets import MUTED_FG, WARN_FG, child_env, open_path, set_state, wheel_sequences

log = logging.getLogger(__name__)

THUMB = 180                  # thumbnail box, in pixels at 100% display scaling
GAP = 12
LABEL_H = 40
WORKERS = 2
LARGE_MAX = 2400             # large previews are decoded no bigger than this
LARGE_CACHE = 6
PUMP_MS = 40

FILTERS: dict[str, Callable[[LibraryShot], bool]] = {
    "All shots": lambda s: True,
    "RAW + JPG": lambda s: "raw" in s.kinds and ("jpg" in s.kinds or "heif" in s.kinds),
    "RAW without JPG": lambda s: "raw" in s.kinds and not ("jpg" in s.kinds
                                                           or "heif" in s.kinds),
    "JPG without RAW": lambda s: "raw" not in s.kinds and ("jpg" in s.kinds
                                                           or "heif" in s.kinds),
    "Videos": lambda s: "video" in s.kinds,
}

_PENDING = object()          # a large preview was requested and is being decoded
_MISSING = object()

_BADGE = {"raw": "RAW", "jpg": "JPG", "heif": "HEIF", "video": "VIDEO", "other": "OTHER",
          "sidecar": "+"}


def summary_text(shots: Sequence[LibraryShot]) -> str:
    """"300 shots · 240 RAW + JPG · 31 RAW without JPG · ..." for the toolbar."""
    parts = [plural(len(shots), "shot")]
    for name, keep in list(FILTERS.items())[1:]:
        n = sum(1 for s in shots if keep(s))
        if n:
            parts.append(f"{n} {name}" if name != "Videos" else plural(n, "video"))
    return " · ".join(parts)


def show_in_folder(path: Path) -> None:
    """Open the file manager with ``path`` selected (its folder where that is not possible)."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.fspath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", os.fspath(path)])
    else:
        subprocess.Popen(["xdg-open", os.fspath(path.parent)], env=child_env())


class PreviewLoader:
    """Worker threads that decode previews; newest (visible) requests are served first."""

    def __init__(self, workers: int = WORKERS) -> None:
        self.results: queue.Queue[tuple[object, Any]] = queue.Queue()
        self._jobs: deque[tuple[object, Callable[[], Any]]] = deque()
        self._cv = threading.Condition()
        self._closed = False
        for n in range(workers):
            threading.Thread(target=self._run, name=f"preview-{n}", daemon=True).start()

    def submit(self, tag: object, work: Callable[[], Any], *, urgent: bool = False) -> None:
        with self._cv:
            if self._closed:
                return
            (self._jobs.appendleft if urgent else self._jobs.append)((tag, work))
            self._cv.notify()

    def clear(self) -> None:
        with self._cv:
            self._jobs.clear()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._jobs.clear()
            self._cv.notify_all()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._jobs and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                tag, work = self._jobs.popleft()
            try:
                result = work()
            except Exception:           # a broken file must not kill the worker
                log.exception("preview failed")
                result = None
            self.results.put((tag, result))


def _first_preview(files: Sequence[LibraryFile], side: int) -> tuple[Preview | None, Path | None]:
    for f in files:
        if f.kind in ("video", "sidecar") or f.online_only:   # never download for a thumb
            continue
        pv = previews.load_preview(f.path, side)
        if pv is not None:
            return pv, f.path
    return None, None


class PhotoViewer(tk.Toplevel):
    """A window over one or more event folders. Read-only."""

    def __init__(self, parent: tk.Misc, folders: Sequence[Path], fmt: FormatTable | None,
                 start: Path | None = None, on_close: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self.title("Photos")
        self.folders = list(folders)
        self.fmt = fmt
        self.on_close = on_close
        self.Image = previews.pillow()
        self.ImageTk: Any = None
        if self.Image is not None:
            try:
                from PIL import ImageTk

                self.ImageTk = ImageTk
            except ImportError:                 # Pillow built without Tk support
                self.Image = None
        self.factor = max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
        self.thumb = int(THUMB * self.factor)
        self.gap = int(GAP * self.factor)
        self.tile_w = self.thumb + self.gap
        self.tile_h = self.thumb + int(LABEL_H * self.factor) + self.gap
        self.loader = PreviewLoader()
        self.gen = 0
        self.folder: Path | None = None
        self.shots: list[LibraryShot] = []
        self.shown: list[LibraryShot] = []
        self.thumbs: dict[str, Any] = {}               # shot key -> PhotoImage
        self.no_preview: set[str] = set()
        self.requested: set[str] = set()
        self.cols = 1
        self.mode = "grid"                             # "grid" or "single" (the large view)
        self.index = 0                                 # current shot in the large view
        self.file: LibraryFile | None = None
        # path -> Preview, None (no preview possible) or _PENDING; newest last
        self.large: OrderedDict[Path, Any] = OrderedDict()
        self.large_photo: Any = None
        self._info_base = ""
        self._resize_job: str | None = None
        self._wheel = wheel_sequences(str(self.tk.call("tk", "windowingsystem")),
                                      str(self.tk.call("package", "provide", "Tk")))
        bold = font.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.bold = bold
        self.small = font.nametofont("TkDefaultFont").copy()
        self.small.configure(size=max(7, int(self.small.cget("size")) - 1))

        w = min(int(1180 * self.factor), self.winfo_screenwidth() - 80)
        h = min(int(800 * self.factor), self.winfo_screenheight() - 120)
        self.geometry(f"{w}x{h}")
        self.minsize(int(640 * self.factor), int(420 * self.factor))
        self._build_grid_page()
        self._build_single_page()
        self.grid_page.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.show_grid())
        self.bind("<Left>", lambda e: self.step(-1))
        self.bind("<Right>", lambda e: self.step(1))
        self.bind("<Home>", lambda e: self.jump(0))
        self.bind("<End>", lambda e: self.jump(len(self.shown) - 1))
        self.after(PUMP_MS, self._pump)
        self.open_folder(start if start is not None else self.folders[0])

    # ---- layout -----------------------------------------------------------------------------

    def _build_grid_page(self) -> None:
        page = self.grid_page = ttk.Frame(self, padding=10)
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        ttk.Label(bar, text="Folder:").pack(side="left")
        self.folder_var = tk.StringVar()
        self.folder_box = ttk.Combobox(bar, textvariable=self.folder_var, state="readonly",
                                       values=[f.name for f in self.folders], width=48)
        self.folder_box.pack(side="left", padx=(6, 16))
        self.folder_box.bind("<<ComboboxSelected>>", self._folder_picked)
        ttk.Label(bar, text="Show:").pack(side="left")
        self.filter_var = tk.StringVar(value="All shots")
        self.filter_box = ttk.Combobox(bar, textvariable=self.filter_var, state="readonly",
                                       values=list(FILTERS), width=18)
        self.filter_box.pack(side="left", padx=(6, 16))
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        ttk.Button(bar, text="Open folder",
                   command=lambda: self._safe(open_path, self.folder)).pack(side="right")
        self.summary = ttk.Label(page, text="", foreground=MUTED_FG)
        self.summary.pack(fill="x", pady=(6, 2))
        self.notice = ttk.Label(page, text="", foreground=WARN_FG, wraplength=900,
                                justify="left")
        if self.Image is None:
            self.notice.configure(text="Previews need the Pillow package (pip install pillow). "
                                       "Click a shot to see its files and open them.")
            self.notice.pack(fill="x", pady=(0, 4))
        body = ttk.Frame(page)
        body.pack(fill="both", expand=True, pady=(4, 0))
        self.canvas = tk.Canvas(body, highlightthickness=0, borderwidth=0, background="#2b2b2b")
        vbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=lambda a, b: (vbar.set(a, b), self.request_visible()))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda e: self.layout())
        self.canvas.tag_bind("tile", "<Button-1>", self._tile_clicked)
        self.canvas.tag_bind("tile", "<Enter>", lambda e: self.canvas.configure(cursor="hand2"))
        self.canvas.tag_bind("tile", "<Leave>", lambda e: self.canvas.configure(cursor=""))
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _build_single_page(self) -> None:
        page = self.single_page = ttk.Frame(self, padding=10)
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        ttk.Button(bar, text="< All shots", command=self.show_grid).pack(side="left")
        ttk.Button(bar, text="Previous", command=lambda: self.step(-1)).pack(side="left",
                                                                              padx=(12, 2))
        ttk.Button(bar, text="Next", command=lambda: self.step(1)).pack(side="left", padx=2)
        self.title_label = ttk.Label(bar, text="", font=self.bold)
        self.title_label.pack(side="left", padx=12)
        self.show_btn = ttk.Button(bar, text="Show in folder",
                                   command=lambda: self._safe(show_in_folder, self.file.path
                                                              if self.file else None))
        self.show_btn.pack(side="right")
        self.open_btn = ttk.Button(bar, text="Open file",
                                   command=lambda: self._safe(open_path, self.file.path
                                                              if self.file else None))
        self.open_btn.pack(side="right", padx=6)
        self.files_bar = ttk.Frame(page)
        self.files_bar.pack(fill="x", pady=(8, 4))
        self.view = tk.Canvas(page, highlightthickness=0, borderwidth=0, background="#1e1e1e")
        self.view.pack(fill="both", expand=True)
        self.view.bind("<Configure>", self._view_resized)
        self.view.bind("<Button-1>", lambda e: self.focus_set())
        self.info = ttk.Label(page, text="", foreground=MUTED_FG)
        self.info.pack(fill="x", pady=(6, 0))
        self.file_var = tk.StringVar()

    # ---- folders and filters ----------------------------------------------------------------

    def open_folder(self, folder: Path) -> None:
        """Show ``folder`` (adds it to the folder list if it is new). Lists on a thread."""
        if folder not in self.folders:
            self.folders.append(folder)
            self.folder_box.configure(values=[f.name for f in self.folders])
        self.folder = folder
        self.folder_var.set(folder.name)
        self.title(f"Photos - {folder.name}")
        self.gen += 1
        self.loader.clear()
        self.shots, self.shown = [], []
        self.thumbs.clear()
        self.no_preview.clear()
        self.requested.clear()
        self.large.clear()
        self.summary.configure(text="Reading the folder...")
        self.show_grid()
        self.layout()
        gen, fmt = self.gen, self.fmt
        self.loader.submit(("list", gen), lambda: previews.list_library(folder, fmt),
                           urgent=True)

    def _folder_picked(self, _event: object) -> None:
        name = self.folder_var.get()
        for f in self.folders:
            if f.name == name and f != self.folder:
                self.open_folder(f)
                return

    def apply_filter(self) -> None:
        keep = FILTERS.get(self.filter_var.get(), FILTERS["All shots"])
        self.shown = [s for s in self.shots if keep(s)]
        self.canvas.yview_moveto(0)
        self.layout()

    # ---- the grid ---------------------------------------------------------------------------

    def _canvas_size(self) -> tuple[int, int]:
        """The grid canvas size; before the window is drawn, estimated from its geometry."""
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w > 1 and h > 1:
            return w, h
        try:
            gw, gh = (int(x) for x in self.wm_geometry().split("+")[0].split("x"))
        except ValueError:
            gw, gh = 1000, 700
        return max(1, gw - 40), max(1, gh - 140)

    def layout(self) -> None:
        c = self.canvas
        c.delete("all")
        width = self._canvas_size()[0]
        self.cols = max(1, (width - self.gap) // self.tile_w)
        rows = (len(self.shown) + self.cols - 1) // self.cols
        c.configure(scrollregion=(0, 0, width, max(rows * self.tile_h + self.gap, 1)))
        if not self.shown:
            text = "No photos here." if self.shots else ""
            c.create_text(width // 2, 60, text=text, fill="#bbbbbb", font=self.bold)
            return
        left = (width - self.cols * self.tile_w + self.gap) // 2
        for i, shot in enumerate(self.shown):
            x = left + (i % self.cols) * self.tile_w
            y = self.gap + (i // self.cols) * self.tile_h
            tags = ("tile", f"t{i}")
            c.create_rectangle(x, y, x + self.thumb, y + self.thumb, fill="#3a3a3a",
                               outline="", tags=tags)
            self._draw_thumb(i, shot, x, y)
            c.create_text(x, y + self.thumb + 4, anchor="nw", text=shot.name, fill="#eeeeee",
                          font=self.bold, tags=tags)
            badges = " ".join(_BADGE.get(k, k.upper()) for k in shot.kinds if k != "sidecar")
            c.create_text(x, y + self.thumb + 4 + int(18 * self.factor), anchor="nw",
                          text=badges, fill="#9ad0ff", font=self.small, tags=tags)
        self.request_visible()

    def _draw_thumb(self, i: int, shot: LibraryShot, x: int, y: int) -> None:
        c = self.canvas
        tags = ("tile", f"t{i}", f"img{i}")
        cx, cy = x + self.thumb // 2, y + self.thumb // 2
        photo = self.thumbs.get(shot.key)
        if photo is not None:
            c.create_image(cx, cy, image=photo, tags=tags)
            return
        if shot.kinds == ["video"] or (set(shot.kinds) <= {"video", "sidecar"}):
            label = "VIDEO\nclick to open"
        elif shot.in_cloud:
            label = "in the cloud\n(not on this PC)"
        elif shot.key in self.no_preview or self.Image is None:
            label = " ".join(_BADGE.get(k, "") for k in shot.kinds) + "\nno preview"
        else:
            label = "..."
        c.create_text(cx, cy, text=label, fill="#9a9a9a", justify="center", tags=tags)

    def request_visible(self) -> None:
        """Queue thumbnails for the rows on screen (plus two below), on-screen ones first."""
        if self.Image is None or not self.shown:
            return
        c = self.canvas
        top, bottom = c.canvasy(0), c.canvasy(self._canvas_size()[1])
        first = max(0, int(top // self.tile_h) * self.cols)
        last = min(len(self.shown), (int(bottom // self.tile_h) + 3) * self.cols)
        gen = self.gen
        for i in range(last - 1, first - 1, -1):     # urgent = LIFO, so submit backwards
            shot = self.shown[i]
            if shot.key in self.requested or shot.key in self.thumbs:
                continue
            files = shot.preview_order()
            if all(f.kind in ("video", "sidecar") or f.online_only for f in files):
                continue
            self.requested.add(shot.key)
            side = self.thumb
            self.loader.submit(("thumb", gen, shot.key),
                               lambda files=files, side=side: _first_preview(files, side)[0],
                               urgent=True)

    def _tile_clicked(self, _event: object) -> None:
        for tag in self.canvas.gettags("current"):
            if tag.startswith("t") and tag[1:].isdigit():
                self.show_single(int(tag[1:]))
                return

    def _bind_wheel(self, _event: object) -> None:
        for seq in self._wheel:
            self.canvas.bind_all(seq, self._on_wheel)

    def _unbind_wheel(self, _event: object) -> None:
        for seq in self._wheel:
            self.canvas.unbind_all(seq)

    def _on_wheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) in (4, 5):
            step = -1 if event.num == 4 else 1
        elif sys.platform == "darwin":
            step = -event.delta
        else:
            step = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(step * 2, "units")

    # ---- the large view ---------------------------------------------------------------------

    def show_grid(self) -> None:
        if self.mode == "single":
            self.mode = "grid"
            self.single_page.pack_forget()
            self.grid_page.pack(fill="both", expand=True)
            self.request_visible()

    def show_single(self, index: int) -> None:
        if not self.shown:
            return
        self.index = max(0, min(index, len(self.shown) - 1))
        if self.mode == "grid":
            self.mode = "single"
            self.grid_page.pack_forget()
            self.single_page.pack(fill="both", expand=True)
        shot = self.shown[self.index]
        self.title_label.configure(text=f"{shot.name}   ({self.index + 1} of "
                                        f"{len(self.shown)})")
        for child in self.files_bar.winfo_children():
            child.destroy()
        ttk.Label(self.files_bar, text="Files:").pack(side="left", padx=(0, 6))
        choice = shot.preview_order()[0]
        self.file_var.set(str(choice.path))
        for f in shot.files:
            ttk.Radiobutton(self.files_bar, text=f"{f.label}  {fmt_bytes(f.size)}",
                            value=str(f.path), variable=self.file_var,
                            command=lambda f=f: self.show_file(f)).pack(side="left", padx=4)
        self.show_file(choice)
        self.focus_set()

    def show_file(self, f: LibraryFile) -> None:
        self.file = f
        self.file_var.set(str(f.path))
        set_state(self.open_btn, True)
        taken = datetime.fromtimestamp(f.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")
        self._info_base = f"{f.path.name} · {fmt_bytes(f.size)} · taken {taken} · {f.folder}/"
        self.info.configure(text=self._info_base)
        if f.online_only:
            self._view_message("This file is in the cloud, not on this PC.\n"
                               "Open file downloads and opens it.")
            return
        if f.kind in ("video", "sidecar") or self.Image is None:
            what = "This is a video: click Open file to play it." if f.kind == "video" else (
                "Previews need the Pillow package (pip install pillow)."
                if self.Image is None else "No preview for this file.")
            self._view_message(what)
            return
        cached = self.large.get(f.path, _MISSING)
        if cached is _MISSING:
            self._view_message("Loading...")
            self.large[f.path] = _PENDING
            self._trim_large()
            self._request_large(f.path, urgent=True)
        elif cached is _PENDING:
            self._view_message("Loading...")
        elif cached is None:
            self._view_message("No preview for this file.")
        else:
            self._render_large()
        self._prefetch()

    def _request_large(self, path: Path, *, urgent: bool) -> None:
        side = LARGE_MAX
        gen = self.gen
        self.loader.submit(("large", gen, path),
                           lambda: previews.load_preview(path, side), urgent=urgent)

    def _prefetch(self) -> None:
        """Decode the next and previous shots in the background for quick flipping."""
        for i in (self.index + 1, self.index - 1):
            if 0 <= i < len(self.shown):
                f = self.shown[i].preview_order()[0]
                if (f.kind not in ("video", "sidecar") and not f.online_only
                        and f.path not in self.large):
                    self.large[f.path] = _PENDING
                    self._trim_large()
                    self._request_large(f.path, urgent=False)

    def _trim_large(self) -> None:
        while len(self.large) > LARGE_CACHE:
            self.large.popitem(last=False)

    def step(self, delta: int) -> None:
        if self.mode == "single":
            self.show_single(self.index + delta)
        else:
            self.canvas.yview_scroll(delta * 3, "units")

    def jump(self, index: int) -> None:
        if self.mode == "single" and self.shown:
            self.show_single(index)

    def _view_message(self, text: str) -> None:
        v = self.view
        v.delete("all")
        self.large_photo = None
        v.create_text(max(1, v.winfo_width()) // 2, max(1, v.winfo_height()) // 2, text=text,
                      fill="#cccccc", font=self.bold, justify="center")

    def _view_resized(self, _event: object) -> None:
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self._render_large)

    def _render_large(self) -> None:
        self._resize_job = None
        f = self.file
        pv = self.large.get(f.path) if f is not None else None
        if f is None or not isinstance(pv, Preview) or self.ImageTk is None:
            return
        v = self.view
        vw, vh = max(1, v.winfo_width()), max(1, v.winfo_height())
        im = pv.image
        scale = min(vw / im.width, vh / im.height, 1.0)
        size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
        shown = im if size == im.size else im.resize(size, self.Image.Resampling.BILINEAR)
        self.large_photo = self.ImageTk.PhotoImage(shown)
        v.delete("all")
        v.create_image(vw // 2, vh // 2, image=self.large_photo)
        self.info.configure(text=f"{self._info_base} · {pv.width} × {pv.height} · "
                                 f"{pv.source}")

    # ---- results from the workers -----------------------------------------------------------

    def _pump(self) -> None:
        try:
            for _ in range(16):
                tag, result = self.loader.results.get_nowait()
                self._handle(tag, result)
        except queue.Empty:
            pass
        except tk.TclError:
            return                              # the window is gone
        self.after(PUMP_MS, self._pump)

    def _handle(self, tag: Any, result: Any) -> None:
        kind, gen = tag[0], tag[1]
        if gen != self.gen:
            return
        if kind == "list":
            self.shots = result or []
            self.summary.configure(text=summary_text(self.shots) if self.shots
                                   else "This folder has no photos or videos.")
            self.apply_filter()
        elif kind == "thumb":
            key = tag[2]
            if result is None:
                self.no_preview.add(key)
            else:
                self.thumbs[key] = self.ImageTk.PhotoImage(result.image)
            self._redraw_tile(key)
        elif kind == "large":
            path = tag[2]
            self.large[path] = result
            self.large.move_to_end(path)
            self._trim_large()
            if self.file is not None and self.file.path == path:
                if result is None:
                    self._view_message("No preview for this file.")
                else:
                    self._render_large()

    def _redraw_tile(self, key: str) -> None:
        for i, shot in enumerate(self.shown):
            if shot.key != key:
                continue
            items = self.canvas.find_withtag(f"img{i}")
            if not items:
                return
            x0, y0, _x1, _y1 = self.canvas.coords(self.canvas.find_withtag(f"t{i}")[0])
            for item in items:
                self.canvas.delete(item)
            self._draw_thumb(i, shot, int(x0), int(y0))
            return

    # ---- misc -------------------------------------------------------------------------------

    def _safe(self, fn: Callable[[Path], None], path: Path | None) -> None:
        if path is None:
            return
        try:
            fn(path)
        except OSError as e:
            from .dialogs import show_error

            show_error(self, "Open", f"Could not open {path}: {e}")

    def close(self) -> None:
        self.loader.close()
        self._unbind_wheel(None)
        if self.on_close is not None:
            self.on_close()
        self.destroy()
