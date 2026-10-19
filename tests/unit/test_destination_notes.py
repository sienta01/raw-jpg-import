"""Cloud/network destination warnings, and delete progress within one file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from raw_jpg_import import volumes
from raw_jpg_import.deleter import DeleteCandidate, DeletionPlan, delete_originals
from raw_jpg_import.events import Progress
from raw_jpg_import.fsops import RealFileOps
from raw_jpg_import.models import Source
from raw_jpg_import.scanner import make_file


@pytest.mark.parametrize("path, expected", [
    (r"C:\Users\me\OneDrive\Pictures\Nikon ZF", "OneDrive"),
    (r"D:\OneDrive - Contoso\Photos", "OneDrive"),
    (r"C:\Users\me\iCloudDrive\Photos", "iCloud Drive"),
    ("/Users/me/Library/Mobile Documents/com~apple~CloudDocs/Photos", "iCloud Drive"),
    ("/Users/me/Dropbox/Photos", "Dropbox"),
    ("/Users/me/Library/CloudStorage/GoogleDrive-me@x.com/My Drive/Photos", "Google Drive"),
    (r"E:\Photos", None),
    ("/home/me/Pictures", None),
])
def test_cloud_folder(path, expected):
    assert volumes.cloud_folder(path) == expected


def test_unc_is_network():
    assert volumes.is_network_path(r"\\nuc2\Users\user\Pictures")
    assert volumes.is_network_path("//nas/photos")


def test_mapped_windows_drive_is_network(monkeypatch):
    class FakeApi:
        def drive_type(self, root):
            return volumes.DRIVE_REMOTE if root.upper().startswith("B:") else volumes.DRIVE_FIXED

    monkeypatch.setattr(volumes, "_platform", lambda: "win32")
    monkeypatch.setattr(volumes, "_winapi", lambda: FakeApi())
    monkeypatch.setattr(volumes.os.path, "abspath", lambda p: os.fspath(p))
    assert volumes.is_network_path(r"B:\Users\user\OneDrive\Pictures")
    assert not volumes.is_network_path(r"D:\Photos")


def test_linux_network_mount(monkeypatch):
    mounts = ("/dev/sda1 / ext4 rw 0 0\n"
              "//nas/photos /mnt/nas cifs rw 0 0\n"
              "/dev/sdb1 /media/me/CARD exfat rw 0 0\n")
    monkeypatch.setattr(volumes, "_platform", lambda: "linux")
    monkeypatch.setattr(volumes, "_read_proc_mounts", lambda: mounts)
    monkeypatch.setattr(volumes.os.path, "abspath", lambda p: os.fspath(p))
    monkeypatch.setattr(volumes.os, "sep", "/")
    assert volumes.is_network_path("/mnt/nas/2026")
    assert not volumes.is_network_path("/home/me/Pictures")


def test_destination_notes_mention_cloud_and_network(monkeypatch):
    monkeypatch.setattr(volumes, "is_network_path", lambda p: True)
    notes = volumes.destination_notes(r"B:\Users\user\OneDrive\Pictures")
    assert len(notes) == 2
    assert "OneDrive" in notes[0] and "finish syncing" in notes[0]
    assert "network" in notes[1]


def test_plain_local_destination_has_no_notes(tmp_path):
    assert volumes.destination_notes(tmp_path) == []


class RecordingOps(RealFileOps):
    def __init__(self):
        super().__init__()
        self.progress_args = []

    def sha256(self, p, progress=None):
        self.progress_args.append(progress)
        return super().sha256(p, progress)


def test_sha256_progress_covers_the_whole_file(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(os.urandom(3 * 1024 * 1024 + 123))
    seen = []
    digest = RealFileOps().sha256(p, seen.append)
    assert sum(seen) == p.stat().st_size and len(seen) == 4
    assert digest == RealFileOps().sha256(p)


def test_delete_reports_progress_inside_a_file(tmp_path, monkeypatch, fmt):
    card = tmp_path / "card" / "DCIM"
    card.mkdir(parents=True)
    src_path = card / "DSC_0001.NEF"
    data = os.urandom(2 * 1024 * 1024)
    src_path.write_bytes(data)
    lib = tmp_path / "lib" / "RAW"
    lib.mkdir(parents=True)
    dest = lib / "DSC_0001.NEF"
    dest.write_bytes(data)
    source = Source(root=tmp_path / "card", label="CARD", kind="card")
    sf = make_file(source, src_path, fmt, os.stat(src_path))
    dplan = DeletionPlan(candidates=[DeleteCandidate(0, sf, dest, None, None)], excluded=[],
                         bytes=sf.size, per_source={"CARD": (1, sf.size)})
    # Every throttle check passes, so each chunk read produces an event.
    monkeypatch.setattr("raw_jpg_import.deleter.Throttle.ready", lambda self, force=False: True)
    events = []
    ops = RecordingOps()
    report = delete_originals(dplan, confirmed=True, ops=ops, emit=events.append)
    assert [r.outcome.name for r in report.results] == ["DELETED"]
    assert all(cb is not None for cb in ops.progress_args) and len(ops.progress_args) == 2
    inside = [e for e in events if isinstance(e, Progress) and e.files_done == 0
              and 0 < e.bytes_done < e.bytes_total]
    assert len(inside) >= 3      # copy and card each read in 1 MiB chunks
    assert [e.bytes_done for e in inside] == sorted(e.bytes_done for e in inside)
    assert dest.exists() and not src_path.exists()


def test_deletion_plan_carries_destination_warnings(monkeypatch):
    from raw_jpg_import import deleter

    monkeypatch.setattr(deleter, "destination_notes", lambda p: ["cloud!"])

    class R:
        plan = type("P", (), {"items": [], "sources": [], "dest_root": Path("x")})()
        results = {}

    assert deleter.plan_deletion(R()).warnings == ["cloud!"]
