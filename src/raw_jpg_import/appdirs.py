"""Per-OS locations for config, data and logs (``APP`` = raw-jpg-import).

- config (config.toml, settings.json):
  Windows %APPDATA%/APP, macOS ~/Library/Application Support/APP, Linux $XDG_CONFIG_HOME/APP
- data (history.sqlite3, import.lock):
  Windows %LOCALAPPDATA%/APP, macOS same as config, Linux $XDG_DATA_HOME/APP
- logs:
  Windows %LOCALAPPDATA%/APP/logs, macOS ~/Library/Logs/APP, Linux $XDG_STATE_HOME/APP/logs

If ``RAW_JPG_IMPORT_HOME`` is set, all three live under that directory (tests use this).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "raw-jpg-import"
HOME_ENV = "RAW_JPG_IMPORT_HOME"


def _home_override() -> Path | None:
    value = os.environ.get(HOME_ENV)
    return Path(value) if value else None


def _platform() -> str:
    return sys.platform


def config_dir() -> Path:
    if (home := _home_override()) is not None:
        return home
    plat = _platform()
    if plat == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def data_dir() -> Path:
    if (home := _home_override()) is not None:
        return home
    plat = _platform()
    if plat == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP


def log_dir() -> Path:
    if (home := _home_override()) is not None:
        return home / "logs"
    plat = _platform()
    if plat == "win32":
        return data_dir() / "logs"
    if plat == "darwin":
        return Path.home() / "Library" / "Logs" / APP
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP / "logs"


def config_file() -> Path:
    return config_dir() / "config.toml"


def settings_file() -> Path:
    return config_dir() / "settings.json"


def ledger_file() -> Path:
    return data_dir() / "history.sqlite3"


def lock_file() -> Path:
    return data_dir() / "import.lock"
