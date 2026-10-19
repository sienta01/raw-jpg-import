"""Background work for the window: one long job at a time plus short "light" jobs.

Only the Tk thread touches widgets or tk Variables. Workers get plain values and talk back
only through :attr:`JobRunner.events`, which ``root.after`` drains on the Tk thread.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..events import CancelToken

log = logging.getLogger(__name__)

PUMP_MS = 50
MAX_EVENTS_PER_TICK = 500


@dataclass(frozen=True)
class JobDone:
    kind: str
    result: Any


@dataclass(frozen=True)
class JobFailed:
    kind: str
    error: BaseException
    details: str


@dataclass(frozen=True)
class LightDone:
    tag: str
    gen: int
    result: Any


@dataclass(frozen=True)
class LightFailed:
    tag: str
    gen: int
    error: BaseException
    details: str


class JobRunner:
    """Runs engine work off the Tk thread and feeds its events back to ``dispatch``.

    ``start`` runs one long, mutually exclusive job (a scan, the import, the delete); it
    gets ``emit`` and ``cancel`` keyword arguments and ends with JobDone or JobFailed.
    ``run_light`` runs a short job (card poll, plan refresh) that may overlap others and
    ends with LightDone or LightFailed carrying the caller's generation number.
    """

    def __init__(self, root: tk.Misc, dispatch: Callable[[object], None]) -> None:
        self.root = root
        self.dispatch = dispatch
        self.events: queue.Queue[object] = queue.Queue()
        self.cancel = CancelToken()
        self.kind: str | None = None
        self.current: threading.Thread | None = None
        self._stopped = False
        self._after: str | None = root.after(PUMP_MS, self._pump)

    @property
    def busy(self) -> bool:
        """A long job is running (cleared when its JobDone/JobFailed is dispatched)."""
        return self.kind is not None

    def start(self, kind: str, fn: Callable[..., Any], *args: Any, **kw: Any) -> CancelToken:
        if self.busy:
            raise RuntimeError(f"busy with {self.kind}")
        self.cancel = token = CancelToken()
        self.kind = kind
        put = self.events.put

        def run() -> None:
            try:
                result = fn(*args, emit=put, cancel=token, **kw)
            except BaseException as e:      # reported on the Tk thread
                put(JobFailed(kind, e, traceback.format_exc()))
            else:
                put(JobDone(kind, result))

        self.current = threading.Thread(target=run, name=f"rji-{kind}", daemon=True)
        self.current.start()
        return token

    def run_light(self, tag: str, fn: Callable[..., Any], gen: int, *args: Any,
                  **kw: Any) -> threading.Thread:
        put = self.events.put

        def run() -> None:
            try:
                result = fn(*args, **kw)
            except BaseException as e:
                put(LightFailed(tag, gen, e, traceback.format_exc()))
            else:
                put(LightDone(tag, gen, result))

        t = threading.Thread(target=run, name=f"rji-{tag}", daemon=True)
        t.start()
        return t

    def request_cancel(self) -> None:
        self.cancel.cancel()

    def stop(self) -> None:
        """Stop pumping (the window is closing). Running daemon threads are abandoned."""
        self._stopped = True
        self.cancel.cancel()
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def pump_now(self) -> int:
        """Dispatch up to MAX_EVENTS_PER_TICK queued events; returns how many."""
        n = 0
        while n < MAX_EVENTS_PER_TICK:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            n += 1
            if isinstance(ev, (JobDone, JobFailed)) and ev.kind == self.kind:
                self.kind = None
            try:
                self.dispatch(ev)
            except Exception:
                log.exception("error while handling %s", type(ev).__name__)
                try:
                    self.root.report_callback_exception(*sys.exc_info())  # type: ignore
                except Exception:
                    pass
            if self._stopped:
                break
        return n

    def _pump(self) -> None:
        self._after = None
        if self._stopped:
            return
        try:
            self.pump_now()
        finally:
            if not self._stopped:
                try:
                    self._after = self.root.after(PUMP_MS, self._pump)
                except tk.TclError:          # the window is gone
                    self._stopped = True
