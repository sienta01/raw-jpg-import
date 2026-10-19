# -*- mode: python -*-
# PyInstaller spec for the RawJpgImport app. Build it with packaging/build.py, which also
# archives the result and can smoke-test it (RawJpgImport --selftest).
#
# Windows and Linux: one windowed executable (dist/RawJpgImport[.exe]).
# macOS: an onedir RawJpgImport.app bundle (PyInstaller 6 deprecates onefile + .app);
#        build.py zips it with ditto so the bundle's symlinks survive.
#
# Icons are optional: packaging/icons/app.ico (Windows) and app.icns (macOS) are used when
# present. Tcl/Tk is collected by PyInstaller's own tkinter hooks. UPX stays off because
# UPX-packed executables trigger antivirus false positives.

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
# The package is normally installed (pip install -e .[build]); this also covers a plain checkout.
sys.path.insert(0, str(SRC))

INIT = (SRC / "raw_jpg_import" / "__init__.py").read_text(encoding="utf-8")
VERSION = re.search(r'^__version__ = "([^"]+)"', INIT, re.M).group(1)

APP_NAME = "RawJpgImport"
# Reverse-DNS id for the macOS bundle; set RJI_BUNDLE_ID once the project has a home.
BUNDLE_ID = os.environ.get("RJI_BUNDLE_ID", "io.github.raw-jpg-import.RawJpgImport")

_icon_name = {"win32": "app.ico", "darwin": "app.icns"}.get(sys.platform)
_icon_path = ROOT / "packaging" / "icons" / _icon_name if _icon_name else None
ICON = str(_icon_path) if _icon_path is not None and _icon_path.is_file() else None

# The GUI imports some modules lazily, so list every submodule explicitly.
hiddenimports = collect_submodules(
    "raw_jpg_import", filter=lambda name: not name.endswith(".__main__"))
# The photo viewer decodes JPEGs with Pillow (imported lazily); the standalone app bundles it.
hiddenimports += ["PIL.Image", "PIL.ImageTk", "PIL.JpegImagePlugin", "PIL._tkinter_finder"]

datas = collect_data_files("raw_jpg_import")      # data/defaults.toml
if sys.platform == "darwin":
    # The .app is zipped on its own, so the licence travels inside it (Contents/Resources).
    datas.append((str(ROOT / "LICENSE"), "."))

a = Analysis(
    [str(ROOT / "packaging" / "launch_gui.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "exifread", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        upx=False,
        icon=ICON,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON,
        version=VERSION,
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            # Shown in the macOS prompt the first time the app reads a memory card.
            "NSRemovableVolumesUsageDescription":
                "Raw/JPG Import reads photos and videos from your memory cards.",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        console=False,
        upx=False,
        icon=ICON,
    )
