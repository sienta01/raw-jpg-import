"""Smoke check for frozen builds: ``RawJpgImport --selftest`` exits 0 when the bundle is whole.

It imports every module, reads the bundled defaults, starts Tcl, parses a tiny EXIF blob,
opens an in-memory SQLite database and, in a frozen build, decodes a JPEG with the bundled
Pillow. It touches no user files.
"""

from __future__ import annotations

import importlib
import struct
import sys

MODULES = [
    "raw_jpg_import.appdirs", "raw_jpg_import.cli", "raw_jpg_import.config",
    "raw_jpg_import.copier", "raw_jpg_import.deleter", "raw_jpg_import.destindex",
    "raw_jpg_import.engine", "raw_jpg_import.events", "raw_jpg_import.formats",
    "raw_jpg_import.fsops", "raw_jpg_import.grouping", "raw_jpg_import.ledger",
    "raw_jpg_import.metadata", "raw_jpg_import.naming", "raw_jpg_import.planner",
    "raw_jpg_import.report", "raw_jpg_import.scanner", "raw_jpg_import.settings",
    "raw_jpg_import.shots", "raw_jpg_import.volumes", "raw_jpg_import.previews",
    "raw_jpg_import.gui.app", "raw_jpg_import.gui.viewer",
]


def _tiny_tiff() -> bytes:
    """Little-endian TIFF with IFD0 holding DateTime = 2026:01:23 17:42:00."""
    value = b"2026:01:23 17:42:00\x00"
    ifd_offset = 8
    entries = 1
    data_offset = ifd_offset + 2 + entries * 12 + 4
    ifd = struct.pack("<H", entries)
    ifd += struct.pack("<HHII", 0x0132, 2, len(value), data_offset)
    ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", ifd_offset) + ifd + value


def run() -> int:
    failures: list[str] = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:
            failures.append(f"import {name}: {e}")
    try:
        from raw_jpg_import.config import load_config

        if not load_config(use_user=False).formats.classify("x.NEF"):
            failures.append("defaults: NEF not classified")
    except Exception as e:
        failures.append(f"defaults: {e}")
    try:
        import tkinter

        tkinter.Tcl().eval("info patchlevel")
    except Exception as e:
        failures.append(f"tcl: {e}")
    try:
        from raw_jpg_import.metadata import read_capture_bytes

        cap = read_capture_bytes(_tiny_tiff(), "selftest.nef")
        if cap.source != "exif" or cap.local.year != 2026:
            failures.append(f"exif: unexpected capture {cap}")
    except Exception as e:
        failures.append(f"exif: {e}")
    try:
        import sqlite3

        sqlite3.connect(":memory:").execute("select 1").fetchone()
    except Exception as e:
        failures.append(f"sqlite: {e}")
    if getattr(sys, "frozen", False):          # the standalone app must bundle the viewer's Pillow
        try:
            import io

            from PIL import Image, ImageTk  # noqa: F401

            buf = io.BytesIO()
            Image.new("RGB", (8, 8)).save(buf, "JPEG")
            Image.open(io.BytesIO(buf.getvalue())).load()
        except Exception as e:
            failures.append(f"pillow: {e}")
    for f in failures:
        print(f"selftest FAILED: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
