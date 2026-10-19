from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from raw_jpg_import import appdirs
from raw_jpg_import.ledger import Ledger, LedgerError, Row, root_key

ROOT = os.path.abspath("Photos Library")
EVENT = "2026.01.23 - Sunset"
T0 = datetime(2026, 1, 23, 23, 2, 11)


def make_row(**kw) -> Row:
    base = Row(
        id=None, session="s1", kind="copied", dest_root=root_key(ROOT), event=EVENT,
        dest_rel="RAW/DSC_0001.NEF", orig_key="dsc_0001.nef", size=1000, mtime_ns=1,
        capture=T0.isoformat(), capture_exact=True, subsec=120, shot_stem_key="dsc_0001",
        sha256="ab" * 32, card_label="NIKON Z F", card_serial="1234", src_rel="DCIM/DSC_0001.NEF")
    return replace(base, **kw)


@pytest.fixture
def led():
    with Ledger.memory() as ledger:
        yield ledger


# ---- find -----------------------------------------------------------------------------------


def test_record_and_find_within_tolerance(led):
    rid = led.record(make_row())
    assert isinstance(rid, int)
    for delta in (-2, 0, 2):
        found = led.find("dsc_0001.nef", 1000, T0 + timedelta(seconds=delta))
        assert [r.id for r in found] == [rid]
    row = found[0]
    assert row.capture == "2026-01-23T23:02:11"
    assert row.capture_exact is True and row.subsec == 120
    assert row.at and row.at[:10] == date.today().isoformat()
    assert row.deleted_at is None


def test_find_outside_tolerance_or_other_size(led):
    led.record(make_row())
    assert led.find("dsc_0001.nef", 1000, T0 + timedelta(seconds=3)) == []
    assert led.find("dsc_0001.nef", 1000, T0 - timedelta(seconds=3)) == []
    assert led.find("dsc_0001.nef", 999, T0) == []
    assert led.find("dsc_0002.nef", 1000, T0) == []
    assert len(led.find("dsc_0001.nef", 1000, T0 + timedelta(seconds=5), tolerance_s=5)) == 1


def test_find_null_capture_matches_name_and_size(led):
    rid = led.record(make_row(capture=None, capture_exact=False, subsec=None))
    assert [r.id for r in led.find("dsc_0001.nef", 1000, T0)] == [rid]
    assert [r.id for r in led.find("dsc_0001.nef", 1000, None)] == [rid]
    assert led.find("dsc_0001.nef", 1001, None) == []


def test_find_unknown_capture_matches_any_row(led):
    rid = led.record(make_row())
    assert [r.id for r in led.find("dsc_0001.nef", 1000, None)] == [rid]


def test_find_newest_first_and_only_imported_kinds(led):
    a = led.record(make_row(kind="copied"))
    b = led.record(make_row(kind="adopted", dest_rel="RAW/DSC_0001-2.NEF"))
    led.record(make_row(kind="deleted_from_card"))
    assert [r.id for r in led.find("dsc_0001.nef", 1000, T0)] == [b, a]


def test_find_aware_capture_uses_wall_clock(led):
    led.record(make_row(capture="2026-01-23T23:02:11+07:00"))
    aware = T0.replace(tzinfo=timezone(timedelta(hours=-5)))
    assert len(led.find("dsc_0001.nef", 1000, aware)) == 1


def test_record_rejects_bad_values(led):
    with pytest.raises(ValueError):
        led.record(make_row(kind="moved"))
    with pytest.raises(ValueError):
        led.record(make_row(capture="yesterday"))


def test_record_does_not_modify_row(led):
    row = make_row(dest_root=ROOT + os.sep)
    led.record(row)
    assert row.id is None and row.at is None and row.dest_root == ROOT + os.sep


# ---- per-event queries ----------------------------------------------------------------------


def test_rows_for_event(led):
    a = led.record(make_row())
    b = led.record(make_row(dest_rel="JPG/DSC_0001.JPG", orig_key="dsc_0001.jpg", size=500))
    led.record(make_row(event="2026.01.24 - Other"))
    led.record(make_row(dest_root=root_key(os.path.abspath("Elsewhere"))))
    led.record(make_row(kind="deleted_from_card"))
    rows = led.rows_for_event(root_key(ROOT), EVENT)
    assert [r.id for r in rows] == [a, b]
    assert rows[1].dest_rel == "JPG/DSC_0001.JPG"
    # a plain path works too: it is turned into a root key
    assert [r.id for r in led.rows_for_event(ROOT + os.sep, EVENT)] == [a, b]


def test_event_names_compared_as_nfc(led):
    nfd = "2026.01.23 - Cafe\u0301"
    rid = led.record(make_row(event=nfd))
    assert [r.id for r in led.rows_for_event(ROOT, "2026.01.23 - Caf\u00e9")] == [rid]


def test_stem_captures(led):
    led.record(make_row())
    led.record(make_row(dest_rel="JPG/DSC_0001.JPG", subsec=None))
    led.record(make_row(shot_stem_key="dsc_0001-2", capture=None, subsec=None,
                        dest_rel="RAW/DSC_0001-2.NEF"))
    led.record(make_row(event="2026.01.24 - Other"))
    assert led.stem_captures(ROOT, EVENT, "dsc_0001") == [(T0, 120), (T0, None)]
    assert led.stem_captures(ROOT, EVENT, "dsc_0001-2") == [(None, None)]
    assert led.stem_captures(ROOT, EVENT, "dsc_0009") == []


def test_days_by_event(led):
    led.record(make_row())
    led.record(make_row(capture="2026-01-24T00:31:00"))
    led.record(make_row(event="2026.02.01", capture=None))
    led.record(make_row(event="2026.03.01", kind="deleted_from_card"))
    led.record(make_row(dest_root=root_key(os.path.abspath("Elsewhere")), event="2026.04.01"))
    assert led.days_by_event(ROOT) == {
        EVENT: {date(2026, 1, 23), date(2026, 1, 24)},
        "2026.02.01": set(),
    }


def test_mark_deleted_keeps_row_imported(led):
    rid = led.record(make_row())
    led.mark_deleted(rid)
    found = led.find("dsc_0001.nef", 1000, T0)
    assert [r.id for r in found] == [rid]
    assert found[0].deleted_at is not None
    first = found[0].deleted_at
    led.mark_deleted(rid)                       # the first deletion time is kept
    assert led.get(rid).deleted_at == first
    led.mark_deleted(9999)                      # unknown id: no error
    assert led.days_by_event(ROOT) == {EVENT: {date(2026, 1, 23)}}


# ---- root_key -------------------------------------------------------------------------------


def test_root_key_normalises():
    nfd = os.path.abspath("Cafe\u0301")
    assert root_key(nfd) == root_key(os.path.abspath("Caf\u00e9"))
    assert root_key(ROOT + os.sep) == root_key(ROOT)
    assert root_key(root_key(ROOT)) == root_key(ROOT)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
def test_root_key_windows():
    assert root_key("D:/Photos/") == "d:\\photos"
    assert root_key("D:\\") == "d:\\"
    assert root_key("d:\\PHOTOS") == root_key("D:/photos")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_root_key_posix():
    assert root_key("/") == "/"
    assert root_key("/media/Photos/") == "/media/Photos"


# ---- files ----------------------------------------------------------------------------------


def test_open_default_path_wal_and_schema(isolated_home):
    with Ledger.open() as led:
        assert led.path == appdirs.ledger_file()
        rid = led.record(make_row())
        assert led.warnings == []
    assert appdirs.ledger_file().is_file()
    conn = sqlite3.connect(appdirs.ledger_file())
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "1"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"meta", "imports", "ix_name", "ix_event"} <= names
    finally:
        conn.close()
    with Ledger.open() as led:                  # reopening keeps the history
        assert [r.id for r in led.find("dsc_0001.nef", 1000, T0)] == [rid]


def test_open_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "history.sqlite3"
    with Ledger.open(path) as led:
        led.record(make_row())
    assert path.is_file()


def test_corrupt_db_set_aside(tmp_path):
    path = tmp_path / "history.sqlite3"
    junk = os.urandom(4096)
    path.write_bytes(junk)
    with Ledger.open(path) as led:
        assert len(led.warnings) == 1 and "history.sqlite3.bad" in led.warnings[0]
        led.record(make_row())
        assert len(led.find("dsc_0001.nef", 1000, T0)) == 1
    assert (tmp_path / "history.sqlite3.bad").read_bytes() == junk
    # a second corruption does not overwrite the first .bad file
    path.write_bytes(b"not a database either" * 100)
    with Ledger.open(path) as led:
        assert led.warnings and led.find("dsc_0001.nef", 1000, T0) == []
    assert (tmp_path / "history.sqlite3.bad").read_bytes() == junk
    assert (tmp_path / "history.sqlite3.bad.2").read_bytes().startswith(b"not a database")


def test_foreign_table_layout_set_aside(tmp_path):
    path = tmp_path / "history.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE imports(id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    with Ledger.open(path) as led:
        assert led.warnings
        led.record(make_row())
    assert (tmp_path / "history.sqlite3.bad").is_file()


def test_newer_schema_is_left_alone(tmp_path):
    path = tmp_path / "history.sqlite3"
    with Ledger.open(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value='2' WHERE key='schema'")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError):
        Ledger.open(path)
    assert not (tmp_path / "history.sqlite3.bad").exists()


def test_context_manager_closes(tmp_path):
    with Ledger.open(tmp_path / "h.sqlite3") as led:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        led.find("x", 1, None)
