"""Low-level file operations behind an injectable interface, plus small OS helpers.

The copier and the deleter only touch files through :class:`FileOps`, so tests can inject
faults. :class:`RealFileOps` never replaces an existing file and rides out the short-lived
locks that antivirus scanners and OneDrive put on freshly written files on Windows.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import shutil
import stat
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO, Protocol, TypeVar

from .errors import AnotherImportRunning
from .volumes import no_error_dialogs

log = logging.getLogger(__name__)

HASH_CHUNK = 1024 * 1024

_T = TypeVar("_T")

# Backoff between attempts while another process holds a fresh file (6 retries, 6.3 s total).
LOCK_RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6, 3.2)
_LOCK_WINERRORS = frozenset({5, 32})     # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
_SHARING_VIOLATION = frozenset({32})

# os.link fails with these where the filesystem has no hard links (exFAT, FAT, SMB, some FUSE).
_NO_LINK_ERRNOS = frozenset(
    e for e in (errno.EPERM, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", None), errno.EXDEV,
                errno.ENOSYS, errno.EMLINK)
    if e is not None)

# Windows file attributes of cloud placeholders whose data is not on this disk.
FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_WIN_ONLINE_ONLY = (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN
                    | FILE_ATTRIBUTE_OFFLINE)
SF_DATALESS = 0x40000000                 # macOS st_flags: iCloud/File Provider placeholder
_IMMUTABLE = stat.UF_IMMUTABLE | stat.SF_IMMUTABLE    # BSD st_flags: file cannot be changed

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# errno values meaning "somebody else holds the lock" (msvcrt gives EACCES, flock EWOULDBLOCK).
_LOCK_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
                               getattr(errno, "EDEADLOCK", errno.EDEADLK), errno.EDEADLK})


def _platform() -> str:
    return sys.platform


class FileOps(Protocol):
    """Every file system call the copier and the deleter make."""

    def stat(self, p: Path) -> os.stat_result: ...
    def lexists(self, p: Path) -> bool: ...
    def open_read(self, p: Path) -> BinaryIO: ...
    def open_excl(self, p: Path) -> BinaryIO: ...
    def fsync(self, f: BinaryIO) -> None: ...
    def sha256(self, p: Path, progress: Callable[[int], None] | None = None) -> str: ...
    def utime(self, p: Path, ns: tuple[int, int]) -> None: ...
    def rename_noreplace(self, src: Path, dst: Path) -> None: ...
    def fsync_dir(self, d: Path) -> None: ...
    def unlink_quiet(self, p: Path) -> None: ...
    def makedirs(self, d: Path) -> list[Path]: ...
    def rmdir_quiet(self, d: Path) -> bool: ...
    def remove(self, p: Path) -> None: ...
    def disk_free(self, p: Path) -> int: ...


def _is_lock_error(e: OSError, winerrors: frozenset[int] = _LOCK_WINERRORS) -> bool:
    return isinstance(e, PermissionError) and getattr(e, "winerror", None) in winerrors


class RealFileOps:
    """The real file system. Subclass it in tests to inject faults."""

    def __init__(self, *, sleep: Callable[[float], None] | None = None) -> None:
        if sleep is not None:
            self.sleep = sleep

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    def _retry_locked(self, fn: Callable[[], _T],
                      winerrors: frozenset[int] = _LOCK_WINERRORS) -> _T:
        """Call ``fn``, retrying while Windows reports the file as in use by another process."""
        for delay in (*LOCK_RETRY_DELAYS, None):
            try:
                return fn()
            except PermissionError as e:
                if delay is None or not _is_lock_error(e, winerrors):
                    raise
                log.debug("file in use, retrying in %.1f s: %s", delay, e)
                self.sleep(delay)
        raise AssertionError("unreachable")

    # -- reading ----------------------------------------------------------------------------

    def stat(self, p: Path) -> os.stat_result:
        return os.stat(p)

    def lexists(self, p: Path) -> bool:
        return os.path.lexists(p)

    def open_read(self, p: Path) -> BinaryIO:
        return open(p, "rb", buffering=0)  # type: ignore[return-value]

    def sha256(self, p: Path, progress: Callable[[int], None] | None = None) -> str:
        """Hash with a fresh open, so the bytes come from the file, not our write buffers.

        ``progress`` is called with the size of each chunk read, so a slow drive (a network
        share, a cloud folder) shows movement within one big file.
        """
        def digest() -> str:
            with open(p, "rb") as f:
                if progress is None:
                    return hashlib.file_digest(f, "sha256").hexdigest()
                h = hashlib.sha256()
                while chunk := f.read(HASH_CHUNK):
                    h.update(chunk)
                    progress(len(chunk))
                return h.hexdigest()

        return self._retry_locked(digest, _SHARING_VIOLATION)

    def disk_free(self, p: Path) -> int:
        return shutil.disk_usage(p).free

    # -- writing ----------------------------------------------------------------------------

    def open_excl(self, p: Path) -> BinaryIO:
        return open(p, "xb")

    def fsync(self, f: BinaryIO) -> None:
        f.flush()
        os.fsync(f.fileno())

    def utime(self, p: Path, ns: tuple[int, int]) -> None:
        os.utime(p, ns=ns)      # timestamps only: never copy the card's read-only flag

    def rename_noreplace(self, src: Path, dst: Path) -> None:
        """Give ``src`` the name ``dst``; raise FileExistsError instead of replacing ``dst``."""
        if _platform() == "win32":
            self._rename_windows(src, dst)
        else:
            self._rename_posix(src, dst)

    def _rename_windows(self, src: Path, dst: Path) -> None:
        # MoveFileExW without MOVEFILE_REPLACE_EXISTING: fails with FileExistsError.
        self._retry_locked(lambda: os.rename(src, dst))

    def _rename_posix(self, src: Path, dst: Path) -> None:
        # os.rename would silently replace dst on POSIX; link() is atomic and refuses to.
        try:
            os.link(src, dst)
        except FileExistsError:
            raise
        except OSError as e:
            if e.errno not in _NO_LINK_ERRNOS:
                raise
            # No hard links here. Check, then rename: the import lock keeps other runs out.
            if os.path.lexists(dst):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(dst)) from e
            os.rename(src, dst)
            return
        try:
            os.unlink(src)
        except OSError as e:     # dst is complete; a leftover temp name is cleaned up later
            log.warning("could not remove temporary name %s: %s", src, e)

    def fsync_dir(self, d: Path) -> None:
        """Make a rename durable (POSIX, best effort). Windows has no directory fsync."""
        if _platform() == "win32":
            return
        try:
            fd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def unlink_quiet(self, p: Path) -> None:
        try:
            self._retry_locked(lambda: os.unlink(p))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("could not delete %s: %s", p, e)

    def makedirs(self, d: Path) -> list[Path]:
        """Create ``d`` and missing parents; return the folders this call created, top-down."""
        missing: list[Path] = []
        p = Path(d)
        while not os.path.isdir(p):
            missing.append(p)
            if p.parent == p:
                break
            p = p.parent
        created: list[Path] = []
        for p in reversed(missing):
            try:
                os.mkdir(p)
            except FileExistsError:
                if not os.path.isdir(p):
                    raise       # a file is in the way
                continue        # created by someone else meanwhile: not ours to remove
            created.append(p)
        return created

    def rmdir_quiet(self, d: Path) -> bool:
        """Remove ``d`` if it is an empty folder. Returns True if it was removed."""
        try:
            os.rmdir(d)
        except (OSError, ValueError):
            return False
        return True

    def remove(self, p: Path) -> None:
        os.remove(p)


# ---- stat helpers ---------------------------------------------------------------------------


def is_online_only(st: os.stat_result) -> bool:
    """True for cloud placeholders (OneDrive, iCloud) whose data would have to be downloaded."""
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is not None:
        return bool(attrs & _WIN_ONLINE_ONLY)
    if _platform() == "darwin":
        return bool(getattr(st, "st_flags", 0) & SF_DATALESS)
    return False


def is_readonly(st: os.stat_result) -> bool:
    """True for files with the read-only flag (the camera's "protect" setting).

    Windows: the read-only attribute. macOS shows the DOS read-only bit of a FAT card as
    the immutable flag (the mode keeps its write bits). Elsewhere: no owner write permission.
    """
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is not None:
        return bool(attrs & FILE_ATTRIBUTE_READONLY)
    if getattr(st, "st_flags", 0) & _IMMUTABLE:
        return True
    return not st.st_mode & 0o200


def same_identity(a: os.stat_result, b: os.stat_result) -> bool:
    """True when two stat results describe the same file or folder (device and file number).

    On Windows these are the volume serial and the file ID, so every spelling of a path
    (``\\\\?\\`` prefix, a ``\\\\localhost\\C$`` share, a junction, a hard link) agrees.
    Filesystems without file numbers (``st_ino == 0``) never match.
    """
    return bool(a.st_ino) and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


# ---- process-wide helpers ------------------------------------------------------------------


def _set_execution_state(flags: int) -> bool:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        fn = kernel32.SetThreadExecutionState
        fn.argtypes = [ctypes.c_uint32]
        fn.restype = ctypes.c_uint32
        return bool(fn(flags))
    except Exception as e:      # keeping the PC awake is a nicety, never a reason to fail
        log.debug("SetThreadExecutionState failed: %s", e)
        return False


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """Stop Windows from sleeping while the block runs (the screen may still turn off).

    A no-op on other systems. Never raises. Enter and exit on the same thread.
    """
    windows = _platform() == "win32"
    if windows:
        _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        if windows:     # always reset: a previous state of 0 is indistinguishable from failure
            _set_execution_state(ES_CONTINUOUS)


def _lock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as e:        # closing the file releases the lock anyway
        log.debug("unlock failed: %s", e)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive OS lock on ``path`` for the block, or raise AnotherImportRunning.

    The OS drops the lock when the process dies, so a crash never leaves a stale lock.
    The lock is per open file, so a second ``file_lock`` in the same process also fails.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:    # never truncates: the file is only a lock handle
        try:
            _lock_fd(f.fileno())
        except OSError as e:
            if e.errno in _LOCK_BUSY_ERRNOS:
                raise AnotherImportRunning() from e
            raise
        try:
            yield
        finally:
            _unlock_fd(f.fileno())


def _resolved(p: Path) -> Path:
    try:
        return Path(p).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(p))


def _strip_long_prefix(s: str) -> str:
    r"""``\\?\C:\x`` -> ``C:\x`` and ``\\?\UNC\srv\share`` -> ``\\srv\share`` (Windows)."""
    head = s[:8].replace("/", "\\").upper()
    if head.startswith(("\\\\?\\", "\\\\.\\")):
        if head[4:8] == "UNC\\":
            return "\\\\" + s[8:]
        if len(s) >= 6 and s[4].isalpha() and s[5] == ":":
            return s[4:]
    return s


def _cmp_key(p: Path) -> str:
    s = str(_resolved(p))
    plat = _platform()
    if plat == "win32":
        return os.path.normcase(_strip_long_prefix(s))
    if plat == "darwin":
        return unicodedata.normalize("NFC", s).casefold()
    return s


def _identity(p: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def same_path(a: Path, b: Path) -> bool:
    """True if ``a`` and ``b`` spell the same path (symlinks resolved, OS case rules)."""
    return _cmp_key(Path(a)) == _cmp_key(Path(b))


def is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or lies inside it.

    Paths are compared resolved and with the OS case rules, then by file identity: when
    ``parent`` exists and ``child`` or one of its ancestors is the same folder, ``child`` is
    inside it whatever the spelling (``\\\\?\\`` prefix, ``\\\\localhost\\C$`` share, a drive
    letter from ``subst``, the case of a name on a FAT card under Linux).
    """
    c, p = _cmp_key(Path(child)), _cmp_key(Path(parent))
    if c == p:
        return True
    prefix = p if p.endswith(os.sep) else p + os.sep
    if c.startswith(prefix):
        return True
    with no_error_dialogs():     # an empty card slot must fail, not pop up a dialog
        target = _identity(Path(parent))
        if target is None:
            return False
        node = _resolved(Path(child))
        return any(_identity(a) == target for a in (node, *node.parents))
