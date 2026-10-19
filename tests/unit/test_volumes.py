from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from raw_jpg_import import volumes
from raw_jpg_import.volumes import SEM_FAILCRITICALERRORS, Volume


def _esc(path: Path | str) -> str:
    """Escape a path the way the kernel writes it in /proc/self/mounts."""
    return str(path).replace("\\", "\\134").replace(" ", "\\040")


def _use_linux(monkeypatch, text: str) -> None:
    monkeypatch.setattr(volumes, "_platform", lambda: "linux")
    monkeypatch.setattr(volumes, "_read_proc_mounts", lambda: text)


# ------------------------------------------------------------------ Linux


def test_parse_mounts_decodes_octal_escapes():
    rows = volumes.parse_mounts(
        "/dev/sdb1 /media/tim/NIKON\\040Z\\040F vfat rw,nosuid 0 0\n"
        "/dev/sdc1 /media/tim/tab\\011and\\134slash exfat rw 0 0\n"
        "garbage\n")
    assert rows == [
        ("/dev/sdb1", "/media/tim/NIKON Z F", "vfat"),
        ("/dev/sdc1", "/media/tim/tab\tand\\slash", "exfat"),
    ]


def test_linux_candidates(monkeypatch):
    text = "\n".join([
        "/dev/sda2 / ext4 rw,relatime 0 0",
        "/dev/sda1 /boot/efi vfat rw 0 0",
        "proc /proc proc rw 0 0",
        "tmpfs /run tmpfs rw 0 0",
        r"/dev/sdb1 /media/tim/NIKON\040Z\040F exfat rw,nosuid 0 0",
        r"/dev/sdc1 /run/media/tim/SD\040CARD vfat rw 0 0",
        "/dev/sdd1 /srv/reader vfat rw 0 0",
        "/dev/sde1 /mnt/usb ext4 rw 0 0",
        "//nas/photos /mnt/nas cifs rw 0 0",
        r"/dev/sdb1 /media/tim/NIKON\040Z\040F exfat rw,nosuid 0 0",
        "/dev/sdf1 /home/tim/data ext4 rw 0 0",
    ])
    _use_linux(monkeypatch, text)
    assert volumes.candidate_mounts() == [
        Path("/media/tim/NIKON Z F"), Path("/run/media/tim/SD CARD"),
        Path("/srv/reader"), Path("/mnt/usb"),
    ]


def test_linux_detect_cards(monkeypatch, tmp_path):
    card = tmp_path / "NIKON Z F"
    (card / "dcim" / "100NCZ_F").mkdir(parents=True)   # lowercase still counts
    usb = tmp_path / "USB"
    (usb / "Documents").mkdir(parents=True)
    fake = tmp_path / "FAKE"
    fake.mkdir()
    (fake / "DCIM").write_bytes(b"")                     # a file, not a folder
    gone = tmp_path / "gone"
    text = "\n".join(f"/dev/sdx{i} {_esc(p)} vfat rw 0 0"
                     for i, p in enumerate([card, usb, fake, gone]))
    _use_linux(monkeypatch, text)

    cards = volumes.detect_cards()
    assert len(cards) == 1
    vol = cards[0]
    assert vol.path == card and vol.label == "NIKON Z F" and vol.is_card
    assert vol.serial is None
    assert vol.total > 0 and vol.free > 0


def test_unreadable_mount_is_skipped(monkeypatch, tmp_path):
    card = tmp_path / "CARD"
    (card / "DCIM").mkdir(parents=True)
    _use_linux(monkeypatch, f"/dev/sdx1 {_esc(card)} vfat rw 0 0")
    real = os.scandir

    def scandir(path="."):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(card)):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert volumes.detect_cards() == []
    assert volumes.volume_for_path(card / "DCIM") is None


def test_volume_for_path_longest_match(monkeypatch, tmp_path):
    outer = tmp_path / "a"
    inner = outer / "b"
    (inner / "DCIM" / "100NCZ_F").mkdir(parents=True)
    (outer / "c").mkdir()
    (tmp_path / "ab").mkdir()
    _use_linux(monkeypatch, f"/dev/sdx1 {_esc(outer)} vfat rw 0 0\n"
                            f"/dev/sdx2 {_esc(inner)} exfat rw 0 0\n")

    vol = volumes.volume_for_path(inner / "DCIM" / "100NCZ_F")
    assert vol is not None and vol.path == inner and vol.is_card
    vol = volumes.volume_for_path(outer / "c")
    assert vol is not None and vol.path == outer and not vol.is_card
    vol = volumes.volume_for_path(str(outer))
    assert vol is not None and vol.path == outer
    assert volumes.volume_for_path(tmp_path / "ab") is None
    assert volumes.volume_for_path(tmp_path) is None


def test_broken_mount_table_does_not_raise(monkeypatch):
    monkeypatch.setattr(volumes, "_platform", lambda: "linux")

    def boom() -> str:
        raise OSError("no /proc")

    monkeypatch.setattr(volumes, "_read_proc_mounts", boom)
    assert volumes.candidate_mounts() == []
    assert volumes.detect_cards() == []
    assert volumes.volume_for_path("/media/x") is None


# ------------------------------------------------------------------ macOS


def test_macos_boot_volume_excluded(monkeypatch, tmp_path):
    vols = tmp_path / "Volumes"
    for name in ("Macintosh HD", "NIKON Z F", "Backup", "not mounted"):
        (vols / name).mkdir(parents=True)
    for name in ("Macintosh HD", "NIKON Z F", "not mounted"):
        (vols / name / "DCIM").mkdir()
    mounted = {"Macintosh HD", "NIKON Z F", "Backup"}
    monkeypatch.setattr(volumes, "_platform", lambda: "darwin")
    monkeypatch.setattr(volumes, "VOLUMES_DIR", str(vols))
    monkeypatch.setattr(volumes, "_ismount", lambda p: os.path.basename(p) in mounted)
    monkeypatch.setattr(volumes, "_realpath",
                        lambda p: "/" if p.endswith("Macintosh HD") else p)

    assert volumes.candidate_mounts() == [vols / "Backup", vols / "NIKON Z F"]
    cards = volumes.detect_cards()
    assert [(c.path, c.label, c.serial, c.is_card) for c in cards] == [
        (vols / "NIKON Z F", "NIKON Z F", None, True)]


def test_macos_without_volumes_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(volumes, "_platform", lambda: "darwin")
    monkeypatch.setattr(volumes, "VOLUMES_DIR", str(tmp_path / "missing"))
    assert volumes.candidate_mounts() == []


# ------------------------------------------------------------------ Windows (mocked)


class FakeWinApi:
    def __init__(self, drives: dict[str, int], info: dict[str, tuple[str, str]] | None = None,
                 mode: int = 0x8000) -> None:
        self.drives = drives
        self.info = info or {}
        self.mode = mode
        self.mode_calls: list[int] = []
        self.modes_seen: list[int] = []

    def logical_drives(self) -> int:
        return sum(1 << (ord(d) - ord("A")) for d in self.drives)

    def drive_type(self, root: str) -> int:
        assert root.endswith("\\")
        self.modes_seen.append(self.mode)
        return self.drives[root[0]]

    def volume_info(self, root: str) -> tuple[str, str] | None:
        self.modes_seen.append(self.mode)
        return self.info.get(root[0])

    def set_thread_error_mode(self, mode: int) -> int | None:
        old, self.mode = self.mode, mode
        self.mode_calls.append(mode)
        return old


DRIVES = {"C": 3, "D": 3, "E": 2, "F": 5, "G": 4, "H": 6, "I": 1, "J": 0}


@pytest.fixture
def winapi(monkeypatch):
    api = FakeWinApi(DRIVES, info={"E": ("NIKON Z F  ", "1A2B-3C4D")})
    monkeypatch.setattr(volumes, "_platform", lambda: "win32")
    monkeypatch.setattr(volumes, "_winapi", lambda: api)
    monkeypatch.setenv("SYSTEMDRIVE", "C:")
    return api


def test_windows_candidates(winapi):
    # C: is the system drive; F: cdrom, G: remote, H: ramdisk, I: no root, J: unknown.
    assert volumes.candidate_mounts() == [Path("D:\\"), Path("E:\\")]
    assert winapi.mode_calls == [SEM_FAILCRITICALERRORS, 0x8000]
    assert winapi.mode == 0x8000
    assert set(winapi.modes_seen) == {SEM_FAILCRITICALERRORS}


def test_windows_other_system_drive(winapi, monkeypatch):
    monkeypatch.setenv("SYSTEMDRIVE", "d:")
    assert volumes.candidate_mounts() == [Path("C:\\"), Path("E:\\")]


def test_windows_detect_cards(winapi, monkeypatch):
    def has_dcim(root: Path) -> bool:
        winapi.modes_seen.append(winapi.mode)
        return str(root).startswith("E")

    monkeypatch.setattr(volumes, "_has_dcim", has_dcim)
    monkeypatch.setattr(volumes, "_usage", lambda p: (64_000, 32_000))
    assert volumes.detect_cards() == [
        Volume(Path("E:\\"), "NIKON Z F", "1A2B-3C4D", 64_000, 32_000, True)]
    assert set(winapi.modes_seen) == {SEM_FAILCRITICALERRORS}
    assert winapi.mode == 0x8000

    if sys.platform != "win32":  # drive-letter paths only parse as such on Windows
        return
    vol = volumes.volume_for_path(Path("D:\\Photos\\2026"))
    assert vol == Volume(Path("D:\\"), "D:", None, 64_000, 32_000, False)
    assert winapi.mode == 0x8000


def test_windows_error_mode_restored_on_failure(winapi, monkeypatch):
    def boom() -> int:
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(winapi, "logical_drives", boom)
    assert volumes.candidate_mounts() == []
    assert volumes.detect_cards() == []
    assert winapi.mode == 0x8000
    assert winapi.mode_calls == [SEM_FAILCRITICALERRORS, 0x8000] * 2


def test_no_error_dialogs_is_noop_off_windows(monkeypatch):
    def fail():
        raise AssertionError("must not touch kernel32")

    monkeypatch.setattr(volumes, "_platform", lambda: "linux")
    monkeypatch.setattr(volumes, "_winapi", fail)
    with volumes.no_error_dialogs():
        pass


# ------------------------------------------------------------------ the real machine


def test_real_detect_cards_does_not_raise(tmp_path):
    cards = volumes.detect_cards()
    assert isinstance(cards, list)
    assert all(isinstance(v, Volume) and v.is_card for v in cards)
    assert all(isinstance(p, Path) for p in volumes.candidate_mounts())
    vol = volumes.volume_for_path(tmp_path)
    assert vol is None or isinstance(vol, Volume)


@pytest.mark.skipif(sys.platform != "win32", reason="real kernel32")
def test_real_winapi_signatures():
    api = volumes._WinApi()
    old = api.set_thread_error_mode(SEM_FAILCRITICALERRORS)
    assert old is not None
    assert api.set_thread_error_mode(old) == SEM_FAILCRITICALERRORS
    system = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    assert api.logical_drives() & (1 << (ord(system[0].upper()) - ord("A")))
    assert api.drive_type(system) == volumes.DRIVE_FIXED
    info = api.volume_info(system)
    assert info is not None and re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", info[1])
    assert system[:2].upper() + "\\" not in [str(p).upper() for p in volumes.candidate_mounts()]
