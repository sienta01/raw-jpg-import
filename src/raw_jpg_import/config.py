"""Load the built-in format table and merge the user's ``config.toml`` over it.

Precedence: built-in defaults < user ``config.toml`` < CLI flags / GUI options.
A bad user config raises :class:`ConfigError`; the engine never falls back silently.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import appdirs
from .errors import ConfigError
from .formats import ROLES, Category, FormatTable
from .naming import key as name_key
from .naming import sanitize_component

SCHEMA = 1

_CATEGORY_KEYS = {"folder", "role", "rank", "extensions", "add", "remove"}
_SCAN_KEYS = {"skip_dirs", "skip_paths", "skip_files"}
_IMPORT_KEYS = {"unknown", "other_folder"}
_TOP_KEYS = {"schema", "categories", "scan", "import"}


@dataclass
class Config:
    formats: FormatTable
    warnings: list[str] = field(default_factory=list)
    user_path: Path | None = None   # the user config that was merged in, if any


def defaults_text() -> str:
    return resources.files("raw_jpg_import").joinpath("data/defaults.toml").read_text("utf-8")


def _loads(text: str, origin: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        m = re.search(r"at line (\d+), column (\d+)", str(e))
        line, col = (int(m.group(1)), int(m.group(2))) if m else (None, None)
        msg = re.sub(r"\s*\(at line \d+, column \d+\)", "", str(e))
        raise ConfigError(f"{origin}: {msg}", line, col) from None


def _norm_ext(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: extensions must be non-empty strings, got {value!r}")
    ext = value.strip().lstrip(".").casefold()
    if not ext or "/" in ext or "\\" in ext or "." in ext:
        raise ConfigError(f"{where}: invalid extension {value!r}")
    return ext


def _ext_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be a list of strings")
    return [_norm_ext(v, where) for v in value]


def _str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return list(value)


def _check_folder(folder: Any, where: str) -> str:
    if not isinstance(folder, str) or not folder:
        raise ConfigError(f"{where}: folder must be a non-empty string")
    clean, _ = sanitize_component(folder)
    if clean != folder:
        raise ConfigError(f"{where}: folder {folder!r} is not a valid folder name")
    return folder


def _check_schema(data: dict[str, Any], origin: str) -> None:
    schema = data.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise ConfigError(f"{origin}: schema = {schema!r} is not supported (expected {SCHEMA})")


def _build(defaults: dict[str, Any], user: dict[str, Any] | None,
           origin: str) -> tuple[FormatTable, list[str]]:
    warnings: list[str] = []
    cats: dict[str, dict[str, Any]] = {}
    for key, spec in defaults["categories"].items():
        cats[key] = {
            "folder": spec.get("folder"),
            "role": spec["role"],
            "rank": spec.get("rank", 9),
            "extensions": [e.casefold() for e in spec["extensions"]],
        }
    scan = {k: list(defaults["scan"].get(k, [])) for k in _SCAN_KEYS}
    imp = dict(defaults["import"])

    if user is not None:
        _check_schema(user, origin)
        for k in user:
            if k not in _TOP_KEYS:
                warnings.append(f"{origin}: unknown setting '{k}' ignored")
        user_cats = user.get("categories", {})
        if not isinstance(user_cats, dict):
            raise ConfigError(f"{origin}: [categories] must be a table")
        claimed: dict[str, str] = {}   # ext -> user category that listed it
        for key, spec in user_cats.items():
            where = f"{origin}: [categories.{key}]"
            if not isinstance(spec, dict):
                raise ConfigError(f"{where} must be a table")
            for k in spec:
                if k not in _CATEGORY_KEYS:
                    warnings.append(f"{where}: unknown setting '{k}' ignored")
            new = key not in cats
            cat = cats.setdefault(key, {"folder": None, "role": None, "rank": 9, "extensions": []})
            if "role" in spec:
                if spec["role"] not in ROLES:
                    raise ConfigError(f"{where}: role must be one of {', '.join(ROLES)}")
                cat["role"] = spec["role"]
            elif new:
                cat["role"] = "primary"
            if "rank" in spec:
                if not isinstance(spec["rank"], int) or isinstance(spec["rank"], bool):
                    raise ConfigError(f"{where}: rank must be an integer")
                cat["rank"] = spec["rank"]
            if "folder" in spec:
                cat["folder"] = _check_folder(spec["folder"], where)
            if "extensions" in spec:
                cat["extensions"] = _ext_list(spec["extensions"], f"{where} extensions")
            if "add" in spec:
                for e in _ext_list(spec["add"], f"{where} add"):
                    if e not in cat["extensions"]:
                        cat["extensions"].append(e)
            if "remove" in spec:
                drop = set(_ext_list(spec["remove"], f"{where} remove"))
                cat["extensions"] = [e for e in cat["extensions"] if e not in drop]
            if cat["role"] == "primary" and not cat["folder"]:
                raise ConfigError(f"{where}: a primary category needs a folder")
            if new and not cat["extensions"]:
                raise ConfigError(f"{where}: a new category needs extensions")
            if "extensions" in spec or "add" in spec:
                listed = set(_ext_list(spec.get("extensions", []), where)) | set(
                    _ext_list(spec.get("add", []), where))
                for e in sorted(listed):
                    if e in claimed and claimed[e] != key:
                        raise ConfigError(
                            f"{origin}: extension '{e}' is claimed by both "
                            f"[categories.{claimed[e]}] and [categories.{key}]")
                    claimed[e] = key
        # A user claim takes the extension away from every other category.
        for e, owner in claimed.items():
            for key, cat in cats.items():
                if key != owner and e in cat["extensions"]:
                    cat["extensions"] = [x for x in cat["extensions"] if x != e]

        user_scan = user.get("scan", {})
        if not isinstance(user_scan, dict):
            raise ConfigError(f"{origin}: [scan] must be a table")
        for k, v in user_scan.items():
            if k not in _SCAN_KEYS:
                warnings.append(f"{origin}: [scan] unknown setting '{k}' ignored")
                continue
            scan[k] = _str_list(v, f"{origin}: [scan] {k}")

        user_imp = user.get("import", {})
        if not isinstance(user_imp, dict):
            raise ConfigError(f"{origin}: [import] must be a table")
        for k, v in user_imp.items():
            if k not in _IMPORT_KEYS:
                warnings.append(f"{origin}: [import] unknown setting '{k}' ignored")
                continue
            imp[k] = v

    if imp.get("unknown") not in ("report", "copy"):
        raise ConfigError(f"{origin}: [import] unknown must be \"report\" or \"copy\"")
    other = _check_folder(imp.get("other_folder"), f"{origin}: [import] other_folder")
    # Unknown files and photos would claim names in the same folder independently.
    for key, cat in cats.items():
        folder = cat["folder"]
        if cat["role"] == "primary" and folder and name_key(folder) == name_key(other):
            raise ConfigError(
                f"{origin}: [import] other_folder {other!r} is also the folder of "
                f"[categories.{key}]; choose a different name for unknown files")

    seen: dict[str, str] = {}
    categories: list[Category] = []
    for key, cat in cats.items():
        for e in cat["extensions"]:
            if e in seen and seen[e] != key:
                raise ConfigError(
                    f"extension '{e}' is in both [categories.{seen[e]}] and [categories.{key}]")
            seen[e] = key
        categories.append(Category(
            key=key,
            folder=cat["folder"] if cat["role"] == "primary" else None,
            role=cat["role"],
            rank=cat["rank"],
            extensions=frozenset(cat["extensions"]),
        ))
    table = FormatTable(
        categories=tuple(categories),
        skip_dirs=tuple(scan["skip_dirs"]),
        skip_paths=tuple(scan["skip_paths"]),
        skip_files=tuple(scan["skip_files"]),
        unknown_policy=imp["unknown"],
        other_folder=other,
    )
    return table, warnings


def load_config(path: Path | None = None, *, use_user: bool = True) -> Config:
    """Built-in defaults merged with the user config (``path`` or the per-OS default).

    A missing user config is fine. ``use_user=False`` returns the built-in table only.
    """
    defaults = _loads(defaults_text(), "built-in defaults")
    user_path = path if path is not None else appdirs.config_file()
    user: dict[str, Any] | None = None
    if use_user and user_path.is_file():
        try:
            text = user_path.read_text("utf-8-sig")
        except OSError as e:
            raise ConfigError(f"{user_path}: cannot read ({e.strerror or e})") from None
        user = _loads(text, str(user_path))
    else:
        user_path = None
    table, warnings = _build(defaults, user, str(user_path) if user_path else "config")
    return Config(formats=table, warnings=warnings, user_path=user_path)


TEMPLATE_HEADER = """\
# raw-jpg-import user configuration.
#
# Everything below is commented out, so the built-in defaults apply until you change something.
# Uncomment and edit only what you want to change. Examples:
#
#   [categories.heif]
#   folder = "HEIF"              # give .HIF files their own folder instead of JPG
#
#   [categories.raw]
#   add = ["cri"]                # add an extension to a category
#   remove = ["dng"]             # stop treating .dng as raw (it becomes "unknown")
#
#   [categories.proxy]           # a new category; takes "lrv" away from "ignore"
#   folder = "PROXY"
#   role = "primary"
#   rank = 4
#   extensions = ["lrv"]
#
#   [import]
#   unknown = "copy"             # copy unknown file types into OTHER/
#
# `extensions` replaces a category's list; `add` and `remove` adjust it.

schema = 1

# ---- built-in defaults (for reference) ----
"""


def template_text() -> str:
    lines = []
    for line in defaults_text().splitlines():
        if line.startswith("schema"):
            continue
        lines.append(f"# {line}" if line.strip() else "")
    return TEMPLATE_HEADER + "\n".join(lines) + "\n"


def write_template(path: Path | None = None) -> Path:
    """Create a commented user config. Refuses to overwrite an existing file."""
    path = path if path is not None else appdirs.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(template_text())
    return path
