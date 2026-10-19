from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import raw_jpg_import
from raw_jpg_import import fsops
from raw_jpg_import.errors import AnotherImportRunning
from raw_jpg_import.fsops import (
    LOCK_RETRY_DELAYS,
    RealFileOps,
    file_lock,
    is_online_only,
    is_readonly,
    is_within,
    keep_awake,
)

SRC_DIR = str(Path(raw_jpg_import.__file__).resolve().parents[1])


def lock_error(winerror: int = 32) -> PermissionError:
    """The PermissionError Windows raises when another process holds the file."""
    if sys.platform == "win32":
        e = OSError(errno.EACCES, "The process cannot access the file", None, winerror)
    else:
        e = PermissionError(errno.EACCES, "The process cannot access the file")
        e.winerror = winerror
    assert isinstance(e, PermissionError) and e.winerror == winerror
    return e


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def ops(sleeps):
    return RealFileOps(sleep=sleeps.append)


def write(p: Path, data: bytes) -> Path:
    p.write_bytes(data)
    return p


# ---- rename_noreplace -----------------------------------------------------------------------


def test_rename_noreplace_moves(tmp_path, ops):
    src = write(tmp_path / ".a.part", b"new")
    dst = tmp_path / "a.nef"
    ops.rename_noreplace(src, dst)
    assert dst.read_bytes() == b"new"
    assert not src.exists()


@pytest.mark.parametrize("how", ["public", "posix"])
def test_rename_never_clobbers(tmp_path, ops, how):
    src = write(tmp_path / ".a.part", b"new")
    dst = write(tmp_path / "a.nef", b"old")
    rename = ops.rename_noreplace if how == "public" else ops._rename_posix
    with pytest.raises(FileExistsError):
        rename(src, dst)
    assert dst.read_bytes() == b"old"
    assert src.read_bytes() == b"new"


def test_posix_link_path_removes_temp_name(tmp_path, ops):
    src = write(tmp_path / ".a.part", b"new")
    dst = tmp_path / "a.nef"
    ops._rename_posix(src, dst)
    assert dst.read_bytes() == b"new"
    assert not src.exists()


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EXDEV])
def test_posix_link_unsupported_falls_back_to_guarded_rename(tmp_path, ops, monkeypatch, code):
    def no_link(a, b):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "link", no_link)
    src = write(tmp_path / ".a.part", b"new")
    dst = tmp_path / "a.nef"
    ops._rename_posix(src, dst)
    assert dst.read_bytes() == b"new"
    assert not src.exists()

    src2 = write(tmp_path / ".b.part", b"newer")
    with pytest.raises(FileExistsError):
        ops._rename_posix(src2, dst)
    assert dst.read_bytes() == b"new"
    assert src2.read_bytes() == b"newer"


def test_posix_link_eperm_permission_error(tmp_path, ops, monkeypatch):
    def no_link(a, b):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    src = write(tmp_path / ".a.part", b"new")
    ops._rename_posix(src, tmp_path / "a.nef")
    assert (tmp_path / "a.nef").read_bytes() == b"new"


def test_posix_link_other_error_propagates(tmp_path, ops, monkeypatch):
    def broken_link(a, b):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "link", broken_link)
    src = write(tmp_path / ".a.part", b"new")
    with pytest.raises(OSError) as ei:
        ops._rename_posix(src, tmp_path / "a.nef")
    assert ei.value.errno == errno.EIO
    assert src.exists() and not (tmp_path / "a.nef").exists()


def flaky(real, failures: int, err=lock_error):
    calls = []

    def fake(*args):
        calls.append(args)
        if len(calls) <= failures:
            raise err()
        return real(*args)

    fake.calls = calls
    return fake


def test_windows_rename_retries_lock_errors(tmp_path, ops, sleeps, monkeypatch):
    fake = flaky(os.rename, failures=2)
    monkeypatch.setattr(os, "rename", fake)
    src = write(tmp_path / ".a.part", b"new")
    ops._rename_windows(src, tmp_path / "a.nef")
    assert (tmp_path / "a.nef").read_bytes() == b"new"
    assert len(fake.calls) == 3
    assert sleeps == [0.1, 0.2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rename path")
def test_rename_noreplace_retries_on_windows(tmp_path, ops, sleeps, monkeypatch):
    monkeypatch.setattr(os, "rename", flaky(os.rename, failures=2))
    src = write(tmp_path / ".a.part", b"new")
    ops.rename_noreplace(src, tmp_path / "a.nef")
    assert (tmp_path / "a.nef").read_bytes() == b"new"
    assert sleeps == [0.1, 0.2]


def test_windows_rename_gives_up_after_six_retries(tmp_path, ops, sleeps, monkeypatch):
    monkeypatch.setattr(os, "rename", flaky(os.rename, failures=99))
    src = write(tmp_path / ".a.part", b"new")
    with pytest.raises(PermissionError):
        ops._rename_windows(src, tmp_path / "a.nef")
    assert sleeps == list(LOCK_RETRY_DELAYS) == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2]
    assert src.exists()


def test_other_permission_errors_not_retried(tmp_path, ops, sleeps, monkeypatch):
    def denied():
        return PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "rename", flaky(os.rename, failures=1, err=denied))
    with pytest.raises(PermissionError):
        ops._rename_windows(write(tmp_path / ".a.part", b"x"), tmp_path / "a.nef")
    assert sleeps == []


def test_default_sleep_uses_time_sleep(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(fsops.time, "sleep", slept.append)
    monkeypatch.setattr(os, "rename", flaky(os.rename, failures=1))

    class Faulty(RealFileOps):
        def __init__(self):     # test doubles may skip RealFileOps.__init__
            pass

    Faulty()._rename_windows(write(tmp_path / ".a.part", b"x"), tmp_path / "a.nef")
    assert slept == [0.1]


# ---- unlink, makedirs, rmdir ----------------------------------------------------------------


def test_unlink_quiet(tmp_path, ops):
    p = write(tmp_path / "a.part", b"x")
    ops.unlink_quiet(p)
    assert not p.exists()
    ops.unlink_quiet(p)                         # already gone
    ops.unlink_quiet(tmp_path)                  # a directory: refused, not raised
    assert tmp_path.is_dir()


def test_unlink_quiet_retries_then_gives_up_silently(tmp_path, ops, sleeps, monkeypatch):
    monkeypatch.setattr(os, "unlink", flaky(os.unlink, failures=99))
    p = write(tmp_path / "a.part", b"x")
    ops.unlink_quiet(p)
    assert p.exists()
    assert sleeps == list(LOCK_RETRY_DELAYS)


def test_unlink_quiet_retry_succeeds(tmp_path, ops, sleeps, monkeypatch):
    monkeypatch.setattr(os, "unlink", flaky(os.unlink, failures=1))
    p = write(tmp_path / "a.part", b"x")
    ops.unlink_quiet(p)
    assert not p.exists()
    assert sleeps == [0.1]


def test_makedirs_returns_created_top_down(tmp_path, ops):
    target = tmp_path / "2026.01.23 - Sunset" / "RAW" / "x"
    created = ops.makedirs(target)
    assert created == [tmp_path / "2026.01.23 - Sunset", tmp_path / "2026.01.23 - Sunset" / "RAW",
                       target]
    assert target.is_dir()
    assert ops.makedirs(target) == []
    assert ops.makedirs(tmp_path / "2026.01.23 - Sunset" / "JPG") == [
        tmp_path / "2026.01.23 - Sunset" / "JPG"]


def test_makedirs_file_in_the_way(tmp_path, ops):
    write(tmp_path / "RAW", b"not a folder")
    with pytest.raises(FileExistsError):
        ops.makedirs(tmp_path / "RAW" / "sub")


def test_rmdir_quiet_only_removes_empty(tmp_path, ops):
    empty = tmp_path / "empty"
    full = tmp_path / "full"
    empty.mkdir()
    full.mkdir()
    write(full / "a.nef", b"x")
    assert ops.rmdir_quiet(empty) is True
    assert not empty.exists()
    assert ops.rmdir_quiet(full) is False
    assert (full / "a.nef").exists()
    assert ops.rmdir_quiet(tmp_path / "missing") is False


# ---- other operations -----------------------------------------------------------------------


def test_reading_writing_and_hashing(tmp_path, ops):
    data = os.urandom(300_000)
    p = tmp_path / ".a.part"
    with ops.open_excl(p) as f:
        f.write(data)
        ops.fsync(f)
    with pytest.raises(FileExistsError):
        ops.open_excl(p)
    with ops.open_read(p) as f:
        assert f.read() == data
    assert ops.sha256(p) == hashlib.sha256(data).hexdigest()
    assert ops.stat(p).st_size == len(data)
    assert ops.lexists(p) and not ops.lexists(tmp_path / "nope")
    assert ops.disk_free(tmp_path) > 0
    ops.fsync_dir(tmp_path)
    ops.remove(p)
    assert not p.exists()


def test_utime_sets_timestamps_only(tmp_path, ops):
    p = write(tmp_path / "a.nef", b"x")
    mtime = 1_769_212_931_000_000_000
    ops.utime(p, (mtime, mtime))
    st = os.stat(p)
    assert st.st_mtime_ns == mtime
    assert not is_readonly(st)


def test_sha256_retries_sharing_violation_only(tmp_path, ops, sleeps, monkeypatch):
    p = write(tmp_path / "a.nef", b"abc")
    monkeypatch.setattr(fsops, "open", flaky(open, failures=1), raising=False)
    assert ops.sha256(p) == hashlib.sha256(b"abc").hexdigest()
    assert sleeps == [0.1]
    monkeypatch.setattr(fsops, "open", flaky(open, failures=1, err=lambda: lock_error(5)),
                        raising=False)
    with pytest.raises(PermissionError):
        ops.sha256(p)
    assert sleeps == [0.1]


# ---- stat helpers ---------------------------------------------------------------------------


@pytest.mark.parametrize(("attrs", "expected"), [
    (0x400000, True),       # RECALL_ON_DATA_ACCESS (OneDrive "online-only")
    (0x40000, True),        # RECALL_ON_OPEN
    (0x1000, True),         # OFFLINE
    (0x400000 | 0x20, True),
    (0x20, False),          # ARCHIVE
    (0x400, False),         # REPARSE_POINT alone (a locally available OneDrive file)
    (0, False),
])
def test_is_online_only_windows_attributes(attrs, expected):
    assert is_online_only(SimpleNamespace(st_file_attributes=attrs, st_mode=0o100666)) is expected


def test_is_online_only_macos_flags(monkeypatch):
    monkeypatch.setattr(fsops, "_platform", lambda: "darwin")
    assert is_online_only(SimpleNamespace(st_flags=0x40000000, st_mode=0o100644)) is True
    assert is_online_only(SimpleNamespace(st_flags=0, st_mode=0o100644)) is False


def test_is_online_only_linux(monkeypatch):
    monkeypatch.setattr(fsops, "_platform", lambda: "linux")
    assert is_online_only(SimpleNamespace(st_flags=0x40000000, st_mode=0o100644)) is False
    assert is_online_only(SimpleNamespace(st_mode=0o100644)) is False


def test_is_online_only_real_local_file(tmp_path):
    assert is_online_only(os.stat(write(tmp_path / "a.nef", b"x"))) is False


def test_is_readonly_real_file(tmp_path):
    p = write(tmp_path / "a.nef", b"x")
    assert is_readonly(os.stat(p)) is False
    os.chmod(p, stat.S_IREAD)
    try:
        assert is_readonly(os.stat(p)) is True
    finally:
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)


def test_is_readonly_fake_stats():
    assert is_readonly(SimpleNamespace(st_file_attributes=0x21, st_mode=0o100666)) is True
    assert is_readonly(SimpleNamespace(st_file_attributes=0x20, st_mode=0o100666)) is False
    assert is_readonly(SimpleNamespace(st_mode=0o100444)) is True
    assert is_readonly(SimpleNamespace(st_mode=0o100644)) is False


# ---- keep_awake -----------------------------------------------------------------------------


def test_keep_awake_does_not_raise():
    with keep_awake():
        pass


def test_keep_awake_sets_and_resets_state(monkeypatch):
    calls = []
    monkeypatch.setattr(fsops, "_platform", lambda: "win32")
    monkeypatch.setattr(fsops, "_set_execution_state", lambda f: calls.append(f) or True)
    with pytest.raises(RuntimeError), keep_awake():
        assert calls == [0x80000001]
        raise RuntimeError("job failed")
    assert calls == [0x80000001, 0x80000000]


def test_keep_awake_noop_elsewhere(monkeypatch):
    calls = []
    monkeypatch.setattr(fsops, "_platform", lambda: "linux")
    monkeypatch.setattr(fsops, "_set_execution_state", calls.append)
    with keep_awake():
        pass
    assert calls == []


def test_keep_awake_survives_missing_api(monkeypatch):
    monkeypatch.setattr(fsops, "_platform", lambda: "win32")
    import ctypes

    monkeypatch.delattr(ctypes, "WinDLL", raising=False)
    with keep_awake():
        pass


# ---- file_lock ------------------------------------------------------------------------------

_PROBE = """
import sys
from pathlib import Path
from raw_jpg_import.errors import AnotherImportRunning
from raw_jpg_import.fsops import file_lock
try:
    with file_lock(Path(sys.argv[1])):
        print("acquired")
except AnotherImportRunning:
    print("busy")
"""


def probe_lock(path: Path) -> str:
    """Try the lock from a separate Python process (the real cross-process case)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (SRC_DIR, env.get("PYTHONPATH")) if p)
    out = subprocess.run([sys.executable, "-c", _PROBE, str(path)], capture_output=True,
                         text=True, timeout=120, env=env, check=True)
    return out.stdout.strip()


def test_file_lock_excludes_other_process(tmp_path):
    path = tmp_path / "data" / "import.lock"
    with file_lock(path):
        assert path.is_file()
        assert probe_lock(path) == "busy"
    assert probe_lock(path) == "acquired"


def test_file_lock_excludes_same_process_and_threads(tmp_path):
    path = tmp_path / "import.lock"
    seen = []

    def other_thread():
        try:
            with file_lock(path):
                seen.append("acquired")
        except AnotherImportRunning:
            seen.append("busy")

    with file_lock(path):
        with pytest.raises(AnotherImportRunning), file_lock(path):
            pass
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        assert seen == ["busy"]
    with file_lock(path):                        # free again after release
        pass
    other_thread()
    assert seen == ["busy", "acquired"]


def test_file_lock_released_on_exception_and_keeps_content(tmp_path):
    path = write(tmp_path / "import.lock", b"keep me")
    with pytest.raises(ValueError), file_lock(path):
        raise ValueError("boom")
    with file_lock(path):
        pass
    assert path.read_bytes() == b"keep me"


# ---- is_within ------------------------------------------------------------------------------


def test_is_within(tmp_path):
    a = tmp_path / "card"
    assert is_within(a / "DCIM" / "100NCZ_F", a)
    assert is_within(a, a)
    assert not is_within(a, a / "DCIM")
    assert not is_within(tmp_path / "card2", a)               # a name prefix is not a parent
    assert is_within(a / "DCIM" / ".." / "MISC", a)
    assert not is_within(a / ".." / "other", a)
    assert is_within(tmp_path, Path(tmp_path.anchor))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows is case-insensitive")
def test_is_within_ignores_case_on_windows(tmp_path):
    a = tmp_path / "Card"
    a.mkdir()
    assert is_within(Path(str(a / "DCIM").upper()), Path(str(a).lower()))
    assert is_within(Path(str(a).replace("\\", "/")), a)


def test_is_within_casefolds_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(fsops, "_platform", lambda: "darwin")
    assert is_within(tmp_path / "LIBRARY" / "Café", tmp_path / "library")
    assert is_within(tmp_path / "Café" / "x", tmp_path / "café")


def test_is_within_case_sensitive_on_linux(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Path.resolve on Windows may adjust case")
    monkeypatch.setattr(fsops, "_platform", lambda: "linux")
    assert not is_within(tmp_path / "LIBRARY" / "x", tmp_path / "library")
