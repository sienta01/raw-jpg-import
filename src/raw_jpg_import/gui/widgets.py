"""Small reusable Tk pieces: a scrollable frame, enable/disable helpers, opening files."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk

ERROR_FG = "#b00020"
WARN_FG = "#8a5a00"
MUTED_FG = "#666666"
OK_FG = "#1b6e20"


def _tk_version(text: str) -> tuple[int, int]:
    m = re.match(r"(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def wheel_sequences(windowing: str, tk_version: str) -> tuple[str, ...]:
    """The events the mouse wheel sends: buttons 4 and 5 on X11 before Tk 8.7 (TIP 474)."""
    if windowing == "x11" and _tk_version(tk_version) < (8, 7):
        return ("<Button-4>", "<Button-5>")
    return ("<MouseWheel>",)


def _wheel_env(widget: tk.Misc) -> tuple[str, str]:
    """``(windowing system, Tk version)`` of the running Tk."""
    try:
        return (str(widget.tk.call("tk", "windowingsystem")),
                str(widget.tk.call("package", "provide", "Tk")))
    except tk.TclError:
        return ("x11" if sys.platform.startswith("linux") else "", "")


class ScrollableFrame(ttk.Frame):
    """A vertically scrolling area: put widgets into ``.inner``.

    The mouse wheel is bound only while the pointer is over the area, so other scrolling
    widgets keep working. Windows reports ``delta`` in steps of 120, macOS raw steps, and
    X11 sends Button-4/5 instead of wheel events before Tk 8.7 (wheel events from 8.7 on).
    """

    def __init__(self, master: tk.Misc, **kw: object) -> None:
        super().__init__(master, **kw)  # type: ignore[arg-type]
        self.on_scroll: Callable[[], None] | None = None   # the visible part moved or resized
        self._wheel = wheel_sequences(*_wheel_env(self))
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)
        self._apply_background()

    def _apply_background(self) -> None:
        try:
            bg = ttk.Style(self).lookup("TFrame", "background") or None
        except tk.TclError:
            bg = None
        if bg:
            self.canvas.configure(background=bg)

    def _on_yscroll(self, first: str, last: str) -> None:
        self.vbar.set(first, last)
        if self.on_scroll is not None:
            self.on_scroll()

    def _on_inner(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        if self.on_scroll is not None:
            self.on_scroll()

    def _bind_wheel(self, _event: tk.Event) -> None:
        for seq in self._wheel:
            self.bind_all(seq, self._on_wheel if seq == "<MouseWheel>" else self._on_wheel_x11)

    def _unbind_wheel(self, _event: tk.Event) -> None:
        for seq in self._wheel:
            self.unbind_all(seq)

    def _can_scroll(self) -> bool:
        first, last = self.canvas.yview()
        return not (first <= 0.0 and last >= 1.0)

    def _on_wheel(self, event: tk.Event) -> None:
        if not self._can_scroll():
            return
        if sys.platform == "darwin":
            step = -event.delta
        else:
            step = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")

    def _on_wheel_x11(self, event: tk.Event) -> None:
        if self._can_scroll():
            self.canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

    def keep_wheel(self, widget: tk.Misc) -> None:
        """Make the wheel over ``widget`` scroll this area instead of changing the widget.

        A ttk combobox otherwise steps through its values on every wheel click, which would
        silently replace an event name while the user scrolls the list.
        """
        def scroll(event: tk.Event) -> str:
            self._on_wheel(event)
            return "break"

        def scroll_x11(event: tk.Event) -> str:
            self._on_wheel_x11(event)
            return "break"

        widget.bind("<MouseWheel>", scroll)
        widget.bind("<Button-4>", scroll_x11)
        widget.bind("<Button-5>", scroll_x11)

    def scroll_top(self) -> None:
        self.canvas.yview_moveto(0)


def set_state(widget: tk.Misc, enabled: bool) -> None:
    """Enable or disable a widget. ttk's "readonly" flag is separate, so it survives."""
    try:
        if isinstance(widget, ttk.Widget):
            widget.state(["!disabled"] if enabled else ["disabled"])
        else:
            widget.configure(state="normal" if enabled else "disabled")  # type: ignore
    except tk.TclError:
        pass


def child_env() -> dict[str, str] | None:
    """The environment for a program the app starts (None: inherit ours unchanged).

    The standalone Linux app (PyInstaller) puts its bundled libraries first on
    LD_LIBRARY_PATH (the original is kept in LD_LIBRARY_PATH_ORIG) and points TCL_LIBRARY /
    TK_LIBRARY into the bundle. A file manager or editor started with that environment loads
    the bundled libraries instead of the system's and may fail to start.
    """
    if not getattr(sys, "frozen", False) or not sys.platform.startswith("linux"):
        return None
    env = dict(os.environ)
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    bundle = getattr(sys, "_MEIPASS", None)
    for var in ("TCL_LIBRARY", "TK_LIBRARY"):
        if bundle and env.get(var, "").startswith(os.fspath(bundle)):
            del env[var]
    return env


def open_path(path: Path | str) -> None:
    """Open a folder or file with the system's default application. Raises OSError."""
    p = os.fspath(path)
    if sys.platform == "win32":
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p], env=child_env())


def open_text_file(path: Path | str) -> None:
    """Open a text file for editing; falls back to Notepad when Windows has no app for it."""
    try:
        open_path(path)
    except OSError:
        if sys.platform != "win32":
            raise
        subprocess.Popen(["notepad.exe", os.fspath(path)])


def label_fg(level: str) -> str:
    return {"error": ERROR_FG, "warning": WARN_FG, "ok": OK_FG, "muted": MUTED_FG}.get(level, "")
