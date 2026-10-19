"""Progress events, cancellation and throttling shared by engine, CLI and GUI.

Engine functions take ``emit`` (a callable receiving event objects) and ``cancel``
(a :class:`CancelToken`). The GUI passes ``queue.put``; the CLI passes a printer.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .errors import Cancelled

Emit = Callable[[object], None]

Phase = Literal["scan", "metadata", "plan", "copy", "verify", "delete"]


def no_emit(_event: object) -> None:
    """Default ``emit`` that drops every event."""


class CancelToken:
    """Thread-safe cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise :class:`Cancelled` if cancellation was requested."""
        if self._event.is_set():
            raise Cancelled()


NEVER_CANCELLED = CancelToken()


@dataclass(frozen=True)
class Progress:
    phase: Phase
    files_done: int
    files_total: int
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""
    rate_bps: float | None = None
    eta_s: float | None = None


@dataclass(frozen=True)
class FileDone:
    item_id: int
    outcome: str  # Outcome.name from copier/deleter
    message: str = ""


@dataclass(frozen=True)
class Notice:
    level: Literal["info", "warning", "error"]
    text: str


class Throttle:
    """Rate-limits Progress events and computes a moving-average transfer rate and ETA.

    ``ready()`` is true at most ``max_per_s`` times per second. The rate is averaged
    over the last ``window_s`` seconds; the ETA is only reported after 3 s or 100 MB.
    """

    def __init__(self, max_per_s: float = 10.0, window_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = 1.0 / max_per_s
        self._window = window_s
        self._clock = clock
        self._last_emit = -1e18
        self._start = clock()
        self._samples: deque[tuple[float, int]] = deque()

    def ready(self, force: bool = False) -> bool:
        now = self._clock()
        if force or now - self._last_emit >= self._interval:
            self._last_emit = now
            return True
        return False

    def sample(self, bytes_done: int) -> None:
        now = self._clock()
        self._samples.append((now, bytes_done))
        while self._samples and now - self._samples[0][0] > self._window:
            self._samples.popleft()

    def rate(self) -> float | None:
        if len(self._samples) < 2:
            return None
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        if t1 - t0 <= 0:
            return None
        return (b1 - b0) / (t1 - t0)

    def eta(self, bytes_done: int, bytes_total: int) -> float | None:
        rate = self.rate()
        elapsed = self._clock() - self._start
        if not rate or rate <= 0 or (elapsed < 3 and bytes_done < 100_000_000):
            return None
        return max(0.0, (bytes_total - bytes_done) / rate)
