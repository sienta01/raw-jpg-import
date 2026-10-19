"""App-owned settings (``settings.json``): last destination, day start, recent names, ...

The app rewrites this file freely; user-edited options live in ``config.toml`` instead.
The "delete originals" choice is deliberately never persisted.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import appdirs

log = logging.getLogger(__name__)

SCHEMA = 1
MAX_RECENT_NAMES = 10
MAX_MANUAL_SOURCES = 5
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass
class Settings:
    schema: int = SCHEMA
    dest_root: str | None = None
    day_start: str = "00:00"
    recent_names: list[str] = field(default_factory=list)
    manual_sources: list[str] = field(default_factory=list)
    hide_imported_days: bool = True
    ask_before_import: bool = True
    window_geometry: str | None = None

    def remember_name(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        self.recent_names = [name] + [n for n in self.recent_names if n != name]
        del self.recent_names[MAX_RECENT_NAMES:]

    def remember_source(self, path: str) -> None:
        self.manual_sources = [path] + [p for p in self.manual_sources if p != path]
        del self.manual_sources[MAX_MANUAL_SOURCES:]


def _valid(name: str, value: object) -> bool:
    if name == "schema":
        return isinstance(value, int)
    if name in ("dest_root", "window_geometry"):
        return value is None or isinstance(value, str)
    if name == "day_start":
        return isinstance(value, str) and bool(_HHMM.match(value))
    if name in ("recent_names", "manual_sources"):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if name in ("hide_imported_days", "ask_before_import"):
        return isinstance(value, bool)
    return False


def load_settings(path: Path | None = None) -> Settings:
    """Read settings; a missing file gives defaults, a corrupt one is set aside as ``.bad``."""
    path = path if path is not None else appdirs.settings_file()
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as e:
        log.warning("cannot read %s: %s; using defaults", path, e)
        return Settings()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except ValueError as e:
        bad = path.with_name(path.name + ".bad")
        try:
            os.replace(path, bad)
        except OSError:
            pass
        log.warning("settings file %s is corrupt (%s); moved to %s, using defaults", path, e, bad)
        return Settings()
    s = Settings()
    for f in fields(Settings):
        if f.name in data and _valid(f.name, data[f.name]):
            setattr(s, f.name, data[f.name])
    s.schema = SCHEMA
    return s


def save_settings(s: Settings, path: Path | None = None) -> None:
    """Write atomically: temp file in the same folder, then ``os.replace``."""
    path = path if path is not None else appdirs.settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(asdict(s), f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
