"""Memory-card detection per OS.

A card is a mounted volume with a ``DCIM`` folder (any case) at its root. Windows asks
kernel32 through ctypes, macOS lists ``/Volumes`` and Linux parses ``/proc/self/mounts``.
Nothing here writes to a volume; detection only lists volume roots.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEM_FAILCRITICALERRORS = 0x0001

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6
# Many USB card readers report their slots as fixed disks, so both count.
_KEEP_DRIVE_TYPES = frozenset({DRIVE_REMOVABLE, DRIVE_FIXED})

VOLUMES_DIR = "/Volumes"
PROC_MOUNTS = "/proc/self/mounts"
DISK_BY_UUID = "/dev/disk/by-uuid"

_LINUX_PREFIXES = ("/media/", "/run/media/", "/mnt/")
_LINUX_CARD_FS = frozenset({"vfat", "exfat", "msdos", "ntfs", "ntfs3", "fuseblk"})
_LINUX_SYSTEM_DIRS = ("/boot", "/efi", "/proc", "/sys", "/dev")
# Listing a network or automount root can hang for a long time; cards are never on these.
_LINUX_SKIP_FS = frozenset({
    "cifs", "smb3", "smbfs", "nfs", "nfs4", "afs", "ceph", "davfs", "fuse.sshfs", "autofs",
})
_OCTAL = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Volume:
    path: Path            # mount point / drive root, e.g. Path("X:/"), Path("/Volumes/NIKON Z F")
    label: str            # volume label, or the folder name if unknown
    serial: str | None    # volume serial ("1A2B-3C4D") if known
    total: int            # bytes (0 if unknown)
    free: int             # bytes (0 if unknown)
    is_card: bool         # True when a DCIM folder (any case) exists at the root


@dataclass(frozen=True)
class _Mount:
    path: Path
    label: str
    serial: str | None = None


def _platform() -> str:
    return sys.platform


# ---------------------------------------------------------------------------- Windows


class _WinApi:
    """The few kernel32 calls used here. Tests replace it through :func:`_winapi`."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        # A private WinDLL instance, so setting argtypes cannot clash with other modules.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        dword_p = ctypes.POINTER(wintypes.DWORD)
        k32.GetLogicalDrives.argtypes = []
        k32.GetLogicalDrives.restype = wintypes.DWORD
        k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        k32.GetDriveTypeW.restype = wintypes.UINT
        k32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, dword_p, dword_p, dword_p,
            wintypes.LPWSTR, wintypes.DWORD,
        ]
        k32.GetVolumeInformationW.restype = wintypes.BOOL
        k32.SetThreadErrorMode.argtypes = [wintypes.DWORD, dword_p]
        k32.SetThreadErrorMode.restype = wintypes.BOOL
        self._ctypes: Any = ctypes
        self._wintypes: Any = wintypes
        self._k32: Any = k32

    def logical_drives(self) -> int:
        """Bitmask of drive letters in use: bit 0 is A:, bit 25 is Z:."""
        return int(self._k32.GetLogicalDrives())

    def drive_type(self, root: str) -> int:
        """``GetDriveTypeW`` for a drive root; it needs the trailing backslash."""
        return int(self._k32.GetDriveTypeW(root))

    def volume_info(self, root: str) -> tuple[str, str] | None:
        """``(label, serial)`` with the serial as ``"1A2B-3C4D"``; None if there is no media."""
        c, w = self._ctypes, self._wintypes
        label = c.create_unicode_buffer(261)
        fs_name = c.create_unicode_buffer(261)
        serial, max_len, flags = w.DWORD(), w.DWORD(), w.DWORD()
        ok = self._k32.GetVolumeInformationW(
            root, label, len(label), c.byref(serial), c.byref(max_len), c.byref(flags),
            fs_name, len(fs_name))
        if not ok:
            return None
        s = serial.value
        return label.value, f"{s >> 16:04X}-{s & 0xFFFF:04X}"

    def set_thread_error_mode(self, mode: int) -> int | None:
        """Set this thread's error mode and return the previous one (None on failure)."""
        old = self._wintypes.DWORD()
        if not self._k32.SetThreadErrorMode(mode, self._ctypes.byref(old)):
            return None
        return int(old.value)


def _winapi() -> _WinApi:
    return _WinApi()


def _swap_error_mode(api: _WinApi, mode: int) -> int | None:
    try:
        return api.set_thread_error_mode(mode)
    except Exception:
        return None


@contextmanager
def no_error_dialogs() -> Iterator[None]:
    """Stop Windows from showing "insert a disk" dialogs on this thread; a no-op elsewhere.

    An empty card-reader slot is a drive without media. Without this, touching it pops up
    a modal system dialog instead of failing with an OSError.
    """
    api: _WinApi | None = None
    old: int | None = None
    if _platform() == "win32":
        try:
            api = _winapi()
        except Exception:
            api = None
        if api is not None:
            old = _swap_error_mode(api, SEM_FAILCRITICALERRORS)
    try:
        yield
    finally:
        if api is not None and old is not None:
            _swap_error_mode(api, old)


def _system_drive() -> str:
    return (os.environ.get("SYSTEMDRIVE") or "C:").rstrip("\\/").upper()


def _windows_mounts(api: _WinApi) -> list[_Mount]:
    mask = api.logical_drives()
    system = _system_drive()
    out: list[_Mount] = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        drive = f"{chr(ord('A') + i)}:"
        if drive == system:
            continue
        root = drive + "\\"
        if api.drive_type(root) not in _KEEP_DRIVE_TYPES:
            continue
        info = api.volume_info(root)
        label, serial = info if info else ("", None)
        # FAT labels are space-padded to 11 characters ("NIKON Z F  ").
        out.append(_Mount(Path(root), label.strip() or drive, serial))
    return out


# ---------------------------------------------------------------------------- macOS


def _ismount(path: str) -> bool:
    return os.path.ismount(path)


def _realpath(path: str) -> str:
    return os.path.realpath(path)


def _darwin_mounts() -> list[_Mount]:
    try:
        with os.scandir(VOLUMES_DIR) as it:
            entries = sorted(it, key=lambda e: e.name.casefold())
    except OSError:
        return []
    out: list[_Mount] = []
    for entry in entries:
        try:
            # The boot volume appears as a link to "/" (e.g. "Macintosh HD").
            if not _ismount(entry.path) or _realpath(entry.path) == "/":
                continue
        except OSError:
            continue
        out.append(_Mount(Path(entry.path), entry.name))
    return out


# ---------------------------------------------------------------------------- Linux


def _unescape(field: str) -> str:
    r"""Decode the octal escapes the kernel uses in mount tables (``\040`` is a space)."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mounts(text: str) -> list[tuple[str, str, str]]:
    """``(device, mountpoint, fstype)`` for each line of a ``/proc/self/mounts`` text."""
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.append((_unescape(parts[0]), _unescape(parts[1]), parts[2]))
    return out


def _is_linux_candidate(mountpoint: str, fstype: str) -> bool:
    if mountpoint == "/" or fstype in _LINUX_SKIP_FS:
        return False
    if any(mountpoint == d or mountpoint.startswith(d + "/") for d in _LINUX_SYSTEM_DIRS):
        return False
    return mountpoint.startswith(_LINUX_PREFIXES) or fstype in _LINUX_CARD_FS


def _linux_serial(device: str) -> str | None:
    """The filesystem UUID from /dev/disk/by-uuid (FAT/exFAT look like ``1A2B-3C4D``)."""
    if not device.startswith("/dev/"):
        return None
    try:
        target = os.path.realpath(device)
        with os.scandir(DISK_BY_UUID) as it:
            for entry in it:
                if os.path.realpath(entry.path) == target:
                    return entry.name
    except OSError:
        return None
    return None


def _read_proc_mounts() -> str:
    with open(PROC_MOUNTS, encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def _linux_mounts(text: str) -> list[_Mount]:
    out: list[_Mount] = []
    seen: set[str] = set()
    for device, mountpoint, fstype in parse_mounts(text):
        if mountpoint in seen or not _is_linux_candidate(mountpoint, fstype):
            continue
        seen.add(mountpoint)
        label = os.path.basename(mountpoint.rstrip("/")) or mountpoint
        out.append(_Mount(Path(mountpoint), label, _linux_serial(device)))
    return out


# ---------------------------------------------------------------------------- common


def _mounts() -> list[_Mount]:
    """Candidate mounts for this OS. Never raises."""
    try:
        plat = _platform()
        if plat == "win32":
            return _windows_mounts(_winapi())
        if plat == "darwin":
            return _darwin_mounts()
        return _linux_mounts(_read_proc_mounts())
    except Exception:
        return []


def _has_dcim(root: Path) -> bool:
    """True if ``root`` holds a DCIM folder (any case). Raises OSError if unlistable."""
    with os.scandir(root) as it:
        for entry in it:
            if entry.name.casefold() != "dcim":
                continue
            try:
                if entry.is_dir():
                    return True
            except OSError:
                continue
    return False


def _usage(path: Path) -> tuple[int, int]:
    try:
        u = shutil.disk_usage(path)
    except (OSError, ValueError):
        return 0, 0
    return u.total, u.free


def _probe(m: _Mount) -> Volume | None:
    """Look at a mount's root; None when it cannot be listed (no media, no permission)."""
    try:
        is_card = _has_dcim(m.path)
    except (OSError, ValueError):
        return None
    total, free = _usage(m.path)
    return Volume(m.path, m.label, m.serial, total, free, is_card)


def candidate_mounts() -> list[Path]:
    """Mounted volumes that could be a camera card (removable or external). Never raises."""
    with no_error_dialogs():
        return [m.path for m in _mounts()]


def detect_cards() -> list[Volume]:
    """Volumes among :func:`candidate_mounts` with DCIM at the root. Never raises."""
    try:
        with no_error_dialogs():
            volumes = [_probe(m) for m in _mounts()]
    except Exception:
        return []
    return [v for v in volumes if v is not None and v.is_card]


def _contains(root: str, target: str) -> bool:
    if target == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return target.startswith(prefix)


def _path_forms(path: Path | str) -> list[str]:
    forms = [os.path.normcase(os.fspath(path))]
    try:
        real = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return forms
    if real not in forms:
        forms.append(real)
    return forms


def volume_for_path(path: Path | str) -> Volume | None:
    """The candidate mount that contains ``path`` (longest match), or None. Never raises.

    Used to give a folder source on a card the card's label and serial. The volume is
    returned whether or not it has a DCIM folder; see ``Volume.is_card``.
    """
    try:
        with no_error_dialogs():
            forms = _path_forms(path)
            best: tuple[int, _Mount] | None = None
            for m in _mounts():
                root = os.path.normcase(os.fspath(m.path))
                if any(_contains(root, f) for f in forms) and (best is None or len(root) > best[0]):
                    best = (len(root), m)
            return _probe(best[1]) if best else None
    except Exception:
        return None


# ---------------------------------------------------------------------------- destinations

# Folder names that sync apps own. Files written there can be moved, renamed, replaced by
# placeholders or removed by the sync app while an import is still running.
_CLOUD_FOLDERS = {
    "onedrive": "OneDrive", "iclouddrive": "iCloud Drive", "icloud drive": "iCloud Drive",
    "mobile documents": "iCloud Drive", "dropbox": "Dropbox", "google drive": "Google Drive",
    "googledrive": "Google Drive", "my drive": "Google Drive", "box": "Box",
}


def cloud_folder(path: Path | str) -> str | None:
    """The sync app ("OneDrive", "Dropbox", ...) whose folder holds ``path``, or None."""
    parts = [p.casefold() for p in Path(os.path.abspath(os.fspath(path))).parts]
    for p in parts:
        if p.startswith("onedrive"):               # "OneDrive", "OneDrive - Company"
            return "OneDrive"
        if p in _CLOUD_FOLDERS:
            return _CLOUD_FOLDERS[p]
    if "cloudstorage" in parts:                    # macOS ~/Library/CloudStorage/<Provider>
        return "a cloud-synced"
    return None


def is_network_path(path: Path | str) -> bool:
    """True for UNC paths, mapped network drives (Windows) and network mounts (Linux)."""
    try:
        p = os.path.abspath(os.fspath(path))
        if p.startswith(("\\\\", "//")):
            return True
        plat = _platform()
        if plat == "win32":
            drive = os.path.splitdrive(p)[0]
            return bool(drive) and _winapi().drive_type(drive + "\\") == DRIVE_REMOTE
        if plat.startswith("linux"):
            best = ("", "")
            for _dev, mountpoint, fstype in parse_mounts(_read_proc_mounts()):
                if _contains(mountpoint, p) and len(mountpoint) >= len(best[0]):
                    best = (mountpoint, fstype)
            return best[1] in _LINUX_SKIP_FS or best[1].startswith("fuse.sshfs")
    except Exception:
        return False
    return False


def destination_notes(path: Path | str) -> list[str]:
    """Warnings worth showing before importing into ``path``, and again before deleting."""
    notes: list[str] = []
    cloud = cloud_folder(path)
    if cloud:
        notes.append(f"The destination is inside a {cloud} folder that syncs to the cloud. "
                     "The sync app can move or remove files while they arrive. Before you "
                     "delete anything from the cards, let it finish syncing and check the "
                     "event folder.")
    if is_network_path(path):
        notes.append("The destination is on a network drive. Every file is copied and later "
                     "checked over the network, which can be slow.")
    return notes
