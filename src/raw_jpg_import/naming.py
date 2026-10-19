"""Event-folder names: sanitising, formatting, parsing and comparison keys.

The same rules apply on every OS so folders sync cleanly between machines.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

MAX_NAME = 80

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DASHES = re.compile(r"-{2,}")
_SPACES = re.compile(r"\s{2,}")
_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

EVENT_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})(?: - (.+))?$")


def key(s: str) -> str:
    """Comparison key for file and folder names: NFC-normalised and casefolded."""
    return unicodedata.normalize("NFC", s).casefold()


def sanitize_component(name: str, maxlen: int = MAX_NAME) -> tuple[str, list[str]]:
    """Make ``name`` safe as a single path component on Windows, macOS and Linux.

    Returns ``(clean, reasons)`` where ``reasons`` explains each change for the UI.
    An empty result (also for ``.`` and ``..``) means "no name".
    """
    reasons: list[str] = []
    s = unicodedata.normalize("NFC", name)
    replaced = _ILLEGAL.sub("-", s)
    if replaced != s:
        bad = sorted({c for c in s if _ILLEGAL.match(c)})
        shown = " ".join(repr(c)[1:-1] if ord(c) < 32 else c for c in bad)
        reasons.append(f"replaced characters not allowed in folder names: {shown}")
    s = _DASHES.sub("-", replaced)
    s = _SPACES.sub(" ", s)
    stripped = s.strip().rstrip(". -").strip()
    if stripped != s and s.strip() != stripped:
        reasons.append("removed trailing dots, spaces or dashes")
    s = stripped
    base, dot, rest = s.partition(".")
    if base.strip().casefold() in _RESERVED:
        s = f"{base}_{dot}{rest}"
        reasons.append(f"'{base}' is a reserved name on Windows")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(". -").strip()
        reasons.append(f"shortened to {maxlen} characters")
    if s in {"", ".", ".."}:
        if name.strip():
            reasons.append("name is empty after cleaning")
        s = ""
    return s, reasons


def format_date(d: date) -> str:
    return f"{d.year:04d}.{d.month:02d}.{d.day:02d}"


def event_folder_name(d: date, name: str) -> str:
    """``2026.01.23 - Sunset Beachwalk``, or ``2026.01.23`` when ``name`` is empty.

    ``name`` is sanitised here, so callers may pass raw user input.
    """
    clean, _ = sanitize_component(name)
    return f"{format_date(d)} - {clean}" if clean else format_date(d)


def parse_event_folder(folder: str) -> tuple[date, str] | None:
    """Inverse of :func:`event_folder_name`; ``None`` for folders not in that format."""
    m = EVENT_RE.match(unicodedata.normalize("NFC", folder))
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return d, (m.group(4) or "")


def parse_user_date(text: str) -> date:
    """Parse ``2026-01-23`` or ``2026.01.23`` (CLI arguments). Raises ``ValueError``."""
    m = re.fullmatch(r"\s*(\d{4})[-.](\d{1,2})[-.](\d{1,2})\s*", text)
    if not m:
        raise ValueError(f"not a date (use YYYY-MM-DD): {text!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
