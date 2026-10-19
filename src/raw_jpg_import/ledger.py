"""SQLite import history (``history.sqlite3``): which card file went to which library file.

The ledger only adds knowledge. If it is missing or unreadable, the disk checks alone still
prevent overwrites and duplicates, so a corrupt database is set aside and replaced, not fatal.

Schema 1 (WAL, synchronous=NORMAL): ``meta(key, value)`` holds ``schema=1``; ``imports`` holds
one row per library file written (``copied``) or found identical (``adopted``). ``capture`` is
the naive camera wall-clock time as ISO text, ``dest_rel`` is POSIX and relative to the event
folder, and ``dest_root`` is :func:`root_key` of the library root.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import TracebackType

from . import appdirs

log = logging.getLogger(__name__)

SCHEMA = 1
LIVE_KINDS = ("copied", "adopted")
KINDS = frozenset({*LIVE_KINDS, "deleted_from_card"})
BUSY_TIMEOUT_S = 10.0

# Primary result codes that mean "this file is not a usable database".
_SQLITE_CORRUPT = 11
_SQLITE_NOTADB = 26

_checked: set[str] = set()     # database files that passed the integrity check in this process

_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS imports(
    id INTEGER PRIMARY KEY,
    session TEXT, at TEXT, kind TEXT,
    dest_root TEXT, event TEXT, dest_rel TEXT,
    orig_key TEXT, size INTEGER, mtime_ns INTEGER,
    capture TEXT, capture_exact INTEGER, subsec INTEGER, shot_stem_key TEXT,
    sha256 TEXT, card_label TEXT, card_serial TEXT, src_rel TEXT,
    deleted_at TEXT);
CREATE INDEX IF NOT EXISTS ix_name ON imports(orig_key, size);
CREATE INDEX IF NOT EXISTS ix_event ON imports(dest_root, event);
"""


class LedgerError(sqlite3.DatabaseError):
    """The history database cannot be used (for example it was written by a newer version)."""


class _Corrupt(sqlite3.DatabaseError):
    """The file is not a healthy schema-1 history; it is set aside and replaced."""


def _casefold_paths() -> bool:
    """macOS: APFS and HFS+ ignore case by default (Windows: normcase; Linux: exact)."""
    return sys.platform == "darwin"


def root_key(dest_root: Path | str) -> str:
    """Comparison key for a library root: absolute, OS case rules, NFC, no trailing separator.

    Idempotent, so a value that already is a root key may be passed again.
    """
    s = os.path.normcase(os.path.abspath(os.fspath(dest_root)))
    s = unicodedata.normalize("NFC", s)
    if _casefold_paths():
        s = s.casefold()
    drive, rest = os.path.splitdrive(s)
    seps = os.sep + (os.altsep or "")
    stripped = rest.rstrip(seps)
    return drive + (stripped or rest[:1])    # keep the separator of a drive or share root


@dataclass
class Row:
    """One ``imports`` row. ``id``, ``at`` and ``deleted_at`` are filled in by the ledger."""

    id: int | None
    session: str
    kind: str
    dest_root: str
    event: str
    dest_rel: str
    orig_key: str
    size: int
    mtime_ns: int
    capture: str | None
    capture_exact: bool
    subsec: int | None
    shot_stem_key: str
    sha256: str | None
    card_label: str
    card_serial: str | None
    src_rel: str
    at: str | None = None
    deleted_at: str | None = None


_COLUMNS = tuple(f.name for f in fields(Row))
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM imports"
_INSERT = (f"INSERT INTO imports({', '.join(_COLUMNS)}) "
           f"VALUES({', '.join('?' for _ in _COLUMNS)})")
_LIVE = f"kind IN ({', '.join(repr(k) for k in LIVE_KINDS)})"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _naive(dt: datetime) -> datetime:
    """Camera wall-clock time: drop any zone without converting."""
    return dt.replace(tzinfo=None)


def _parse_capture(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return _naive(datetime.fromisoformat(text))
    except ValueError:
        return None


def _normalize_capture(text: str | None) -> str | None:
    if text is None or text == "":
        return None
    dt = _parse_capture(text)
    if dt is None:
        raise ValueError(f"capture is not an ISO date-time: {text!r}")
    return dt.isoformat(timespec="seconds")


def _to_row(values: tuple) -> Row:
    row = Row(*values)
    row.capture_exact = bool(row.capture_exact)
    return row


def _is_corruption(e: sqlite3.DatabaseError) -> bool:
    if isinstance(e, _Corrupt):
        return True
    code = getattr(e, "sqlite_errorcode", None)     # "database is locked" etc. are not corruption
    return code is not None and code & 0xFF in (_SQLITE_CORRUPT, _SQLITE_NOTADB)


def _bad_name(path: Path) -> Path:
    bad = path.with_name(path.name + ".bad")
    n = 2
    while bad.exists():
        bad = path.with_name(f"{path.name}.bad.{n}")
        n += 1
    return bad


def _set_aside(path: Path) -> Path:
    """Rename a corrupt database (and its WAL) out of the way; returns the new name."""
    bad = _bad_name(path)
    os.rename(path, bad)
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        try:
            os.rename(wal, bad.with_name(bad.name + "-wal"))
        except OSError:
            os.remove(wal)      # a stale WAL must never be replayed into the new database
    try:
        os.remove(path.with_name(path.name + "-shm"))
    except FileNotFoundError:
        pass
    return bad


class Ledger:
    """Import history. One instance per thread: sqlite3 connections stay on their thread."""

    def __init__(self, conn: sqlite3.Connection, path: Path | None = None) -> None:
        self._conn = conn
        self.path = path
        self.warnings: list[str] = []

    # -- opening ----------------------------------------------------------------------------

    @classmethod
    def open(cls, path: Path | None = None) -> Ledger:
        """Open (or create) the history at ``path`` (default: the per-user data folder).

        A corrupt file is renamed ``*.bad`` and a new, empty history is created in its place;
        the message is in ``warnings``.
        """
        path = Path(path) if path is not None else appdirs.ledger_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return cls._connect(path)
        except sqlite3.DatabaseError as e:
            if not _is_corruption(e):
                raise
            reason = str(e)
        bad = _set_aside(path)
        log.warning("import history %s is corrupt (%s); moved to %s and started a new one",
                    path, reason, bad)
        ledger = cls._connect(path)
        ledger.warnings.append(
            f"The import history was damaged ({reason}). It was moved to {bad.name} and a new "
            "one was started; files removed from the library before today may be offered "
            "for import again.")
        return ledger

    @classmethod
    def memory(cls) -> Ledger:
        """A throwaway in-memory history (tests, or when the real one is unavailable)."""
        conn = sqlite3.connect(":memory:")
        try:
            _init_schema(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    @classmethod
    def _connect(cls, path: Path) -> Ledger:
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
            conn.execute("PRAGMA synchronous=NORMAL")
            _check_integrity(conn, path)
            _init_schema(conn)
            _rekey_roots(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn, path)

    # -- queries ----------------------------------------------------------------------------

    def find(self, orig_key: str, size: int, capture: datetime | None, *,
             tolerance_s: float = 2) -> list[Row]:
        """Imported rows for a card file name key and size, newest first.

        When both captures are known they must be within ``tolerance_s``; rows recorded
        without a capture match on name and size alone.
        """
        cur = self._conn.execute(
            f"{_SELECT} WHERE orig_key = ? AND size = ? AND {_LIVE} ORDER BY id DESC",
            (orig_key, size))
        rows = [_to_row(v) for v in cur]
        if capture is None:
            return rows
        want, tol = _naive(capture), timedelta(seconds=tolerance_s)
        return [r for r in rows
                if (dt := _parse_capture(r.capture)) is None or abs(dt - want) <= tol]

    def rows_for_event(self, dest_root: Path | str, event: str) -> list[Row]:
        """Imported rows of one event folder, oldest first."""
        cur = self._conn.execute(
            f"{_SELECT} WHERE dest_root = ? AND event = ? AND {_LIVE} ORDER BY id",
            (root_key(dest_root), _nfc(event)))
        return [_to_row(v) for v in cur]

    def stem_captures(self, dest_root: Path | str, event: str,
                      stem_key: str) -> list[tuple[datetime | None, int | None]]:
        """``(capture, subsec)`` of imported rows in that event whose stem key is ``stem_key``."""
        cur = self._conn.execute(
            f"SELECT capture, subsec FROM imports WHERE dest_root = ? AND event = ? "
            f"AND shot_stem_key = ? AND {_LIVE} ORDER BY id",
            (root_key(dest_root), _nfc(event), stem_key))
        return [(_parse_capture(c), s) for c, s in cur]

    def days_by_event(self, dest_root: Path | str,
                      day_start: time = time(0)) -> dict[str, set[date]]:
        """Event folder name -> days imported into it (empty set if none known).

        A capture counts for the day it belongs to when days start at ``day_start`` (as in
        ``grouping.day_key``), so a party folder that runs past midnight does not also claim
        the next day.
        """
        shift = f"-{day_start.hour * 60 + day_start.minute} minutes"
        cur = self._conn.execute(
            f"SELECT DISTINCT event, date(capture, ?) FROM imports "
            f"WHERE dest_root = ? AND {_LIVE}",
            (shift, root_key(dest_root)))
        days: dict[str, set[date]] = {}
        for event, day in cur:
            dates = days.setdefault(event, set())
            try:
                dates.add(date.fromisoformat(day))
            except (TypeError, ValueError):
                pass        # imported without a known capture time
        return days

    # -- writes -----------------------------------------------------------------------------

    def record(self, row: Row) -> int:
        """Insert ``row`` (committed at once) and return its id. ``row`` is not modified."""
        if row.kind not in KINDS:
            raise ValueError(f"unknown ledger row kind: {row.kind!r}")
        values = {c: getattr(row, c) for c in _COLUMNS}
        values.update(
            dest_root=root_key(row.dest_root), event=_nfc(row.event),
            capture=_normalize_capture(row.capture), capture_exact=int(bool(row.capture_exact)),
            at=row.at or _now())
        with self._conn:
            cur = self._conn.execute(_INSERT, tuple(values[c] for c in _COLUMNS))
        row_id = cur.lastrowid
        assert row_id is not None
        return row_id

    def mark_deleted(self, row_id: int) -> None:
        """Note that the card original of this row was deleted. The row stays imported."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE imports SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (_now(), row_id))
        if cur.rowcount == 0:
            log.debug("mark_deleted: no undeleted ledger row %s", row_id)

    def get(self, row_id: int) -> Row | None:
        cur = self._conn.execute(f"{_SELECT} WHERE id = ?", (row_id,))
        values = cur.fetchone()
        return _to_row(values) if values is not None else None

    # -- lifetime ---------------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _check_integrity(conn: sqlite3.Connection, path: Path) -> None:
    """Raise _Corrupt unless the file passes a quick check (once per path and process).

    The check reads the whole file, and the GUI opens the history on every plan refresh.
    """
    k = os.path.normcase(str(path))
    if k in _checked:
        return
    result = conn.execute("PRAGMA quick_check(1)").fetchone()
    if result is None or result[0] != "ok":
        raise _Corrupt(f"database disk image is malformed: {result and result[0]}")
    _checked.add(k)


_ROOT_KEYS = "root_keys"            # meta key: "casefold" once rows were re-keyed for macOS


def _rekey_roots(conn: sqlite3.Connection) -> None:
    """macOS: once, re-key rows written before root keys were case-folded.

    The only write on opening an existing history; when the file is busy or read-only it
    is simply tried again next time.
    """
    if not _casefold_paths():
        return
    done = conn.execute("SELECT value FROM meta WHERE key = ?", (_ROOT_KEYS,)).fetchone()
    if done is not None and done[0] == "casefold":
        return
    try:
        with conn:
            old = [r[0] for r in conn.execute("SELECT DISTINCT dest_root FROM imports")]
            for value in old:
                if value is not None and (new := root_key(value)) != value:
                    conn.execute("UPDATE imports SET dest_root = ? WHERE dest_root = ?",
                                 (new, value))
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, 'casefold')",
                         (_ROOT_KEYS,))
    except sqlite3.OperationalError as e:
        log.debug("could not re-key the import history yet: %s", e)


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if needed and check the schema version and columns."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, needed in (("meta", ("key", "value")), ("imports", _COLUMNS)):
        if table in tables:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if missing := set(needed) - have:
                raise _Corrupt(f"{table} table lacks columns {', '.join(sorted(missing))}")
    if not {"meta", "imports"} <= tables:
        # Only a new file gets here, so opening an existing history never needs a write lock.
        conn.executescript(
            f"BEGIN; {_DDL} INSERT OR IGNORE INTO meta(key, value) VALUES('schema', '{SCHEMA}'); "
            "COMMIT;")
    found = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    try:
        version = int(found[0])
    except (TypeError, ValueError):
        raise _Corrupt(f"malformed schema version {found!r}") from None
    if version > SCHEMA:
        raise LedgerError(f"the import history was written by a newer version (schema {version})")
