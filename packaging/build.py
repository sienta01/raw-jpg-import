"""Build the standalone RawJpgImport app with PyInstaller, smoke-test it and archive it.

    python packaging/build.py                         # app in dist/, temporary files in build/
    python packaging/build.py --smoke                 # ... and run the app's --selftest
    python packaging/build.py --out C:/rji-build      # build outside the repo (needed in OneDrive)

The app goes to ``<out>/dist`` and the archives to ``<out>/dist/release``; ``<out>`` is
``--out``, else ``$RJI_BUILD_DIR``, else the repository:

    Windows  RawJpgImport-<ver>-windows-x64.zip    RawJpgImport.exe, LICENSE.txt, README.md
    macOS    RawJpgImport-<ver>-macos-arm64.zip    RawJpgImport.app (zipped with ditto)
    Linux    RawJpgImport-<ver>-linux-x64.tar.gz   RawJpgImport, raw-jpg-import.desktop, LICENSE,
                                                   README.md

Each archive gets a ``<archive>.sha256`` file in ``sha256sum`` format. Stdlib only; PyInstaller
comes from the build extra: pip install -e ".[build]"
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "raw_jpg_import.spec"
DESKTOP_FILE = ROOT / "packaging" / "linux" / "raw-jpg-import.desktop"
LINUX_ICON = ROOT / "packaging" / "icons" / "app.png"
APP = "RawJpgImport"
BUILD_DIR_ENV = "RJI_BUILD_DIR"
SMOKE_TIMEOUT_S = 300
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ARCH = {"amd64": "x64", "x86_64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64"}


class BuildError(Exception):
    """A build step failed; the message says which and what to do."""


@dataclass(frozen=True)
class Layout:
    dist: Path       # PyInstaller --distpath: the finished app
    work: Path       # PyInstaller --workpath: temporary build files
    release: Path    # archives and their checksums


def make_layout(out: str | None) -> Layout:
    base_text = out or os.environ.get(BUILD_DIR_ENV)
    base = Path(base_text).expanduser().resolve() if base_text else ROOT
    dist = base / "dist"
    return Layout(dist=dist, work=base / "build", release=dist / "release")


def read_version() -> str:
    text = (ROOT / "src" / "raw_jpg_import" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if m is None:
        raise BuildError("__version__ not found in src/raw_jpg_import/__init__.py")
    return m.group(1)


def os_name(plat: str | None = None) -> str:
    plat = sys.platform if plat is None else plat
    if plat == "win32":
        return "windows"
    if plat == "darwin":
        return "macos"
    if plat.startswith("linux"):
        return "linux"
    raise BuildError(f"unsupported platform: {plat}")


def default_platform_tag() -> str:
    machine = platform.machine().lower()
    return f"{os_name()}-{_ARCH.get(machine, machine or 'unknown')}"


def in_onedrive(path: Path) -> bool:
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and path.is_relative_to(Path(root)):
            return True
    # "OneDrive" (personal) or "OneDrive - Company" (work or school) anywhere in the path.
    return any(p.casefold() == "onedrive" or p.casefold().startswith("onedrive - ")
               for p in path.parts)


def warn_if_synced(layout: Layout) -> None:
    if in_onedrive(layout.work) or in_onedrive(layout.dist):
        print("warning: building inside a OneDrive folder. OneDrive locks files while it syncs, "
              "which makes PyInstaller fail with WinError 32. Build outside it, for example:\n"
              "    python packaging/build.py --out %LOCALAPPDATA%\\rji-build\n"
              f"or set {BUILD_DIR_ENV} to a folder outside OneDrive.", file=sys.stderr)


# ---- build ----------------------------------------------------------------------------------


def require_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise BuildError("PyInstaller is not installed in this Python. Install the build "
                         "extra first:\n    pip install -e \".[build]\"")


def run_pyinstaller(layout: Layout) -> None:
    import PyInstaller.__main__

    layout.dist.mkdir(parents=True, exist_ok=True)
    PyInstaller.__main__.run([str(SPEC), "--noconfirm", "--clean",
                              "--distpath", str(layout.dist), "--workpath", str(layout.work)])


def built_app(layout: Layout) -> Path:
    """The .exe, the single Linux binary, or the macOS .app bundle."""
    name = {"windows": f"{APP}.exe", "macos": f"{APP}.app", "linux": APP}[os_name()]
    return layout.dist / name


def app_executable(app: Path) -> Path:
    if os_name() == "macos":
        return app / "Contents" / "MacOS" / APP
    return app


# ---- smoke test -----------------------------------------------------------------------------


def smoke_command(exe: Path) -> list[str]:
    cmd = [str(exe), "--selftest"]
    # The self-test only starts Tcl, but a display keeps Tk happy if a hook ever asks for one.
    if os_name() == "linux" and not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", *cmd]
    return cmd


def smoke_test(exe: Path) -> None:
    cmd = smoke_command(exe)
    print("smoke test:", " ".join(cmd), flush=True)
    with tempfile.TemporaryDirectory(prefix="rji-smoke-", ignore_cleanup_errors=True) as tmp:
        log = Path(tmp) / "selftest.log"
        env = dict(os.environ, RJI_SELFTEST_LOG=str(log),
                   RAW_JPG_IMPORT_HOME=str(Path(tmp) / "home"))
        try:
            proc = subprocess.run(cmd, env=env, timeout=SMOKE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise BuildError(f"the self-test did not finish within {SMOKE_TIMEOUT_S} s") from None
        except OSError as e:
            raise BuildError(f"cannot run {exe}: {e}") from None
        output = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    if output.strip():
        print(output.rstrip())
    if proc.returncode != 0:
        raise BuildError(f"the self-test failed (exit code {proc.returncode})")
    print("smoke test passed")


# ---- archives -------------------------------------------------------------------------------


@contextlib.contextmanager
def _writing(dest: Path) -> Iterator[Path]:
    """Write to a temporary name and move it into place only when complete."""
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        yield tmp
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def zip_windows(exe: Path, dest: Path) -> None:
    with _writing(dest) as tmp, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                                                compresslevel=9) as z:
        z.write(exe, exe.name)
        z.write(ROOT / "LICENSE", "LICENSE.txt")
        z.write(ROOT / "README.md", "README.md")


def zip_macos(app: Path, dest: Path) -> None:
    # ditto keeps the bundle's symlinks and metadata, which zipfile would flatten.
    ditto = shutil.which("ditto") or "/usr/bin/ditto"
    with _writing(dest) as tmp:
        subprocess.run([ditto, "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(tmp)],
                       check=True)


def tar_linux(binary: Path, dest: Path, top: str) -> None:
    members = [(binary, APP, 0o755), (DESKTOP_FILE, DESKTOP_FILE.name, 0o644),
               (ROOT / "LICENSE", "LICENSE", 0o644), (ROOT / "README.md", "README.md", 0o644)]
    if LINUX_ICON.is_file():
        members.append((LINUX_ICON, "raw-jpg-import.png", 0o644))
    with _writing(dest) as tmp, tarfile.open(tmp, "w:gz") as tar:
        folder = tarfile.TarInfo(top)
        folder.type, folder.mode, folder.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
        tar.addfile(folder)
        for src, name, mode in members:
            info = tar.gettarinfo(str(src), arcname=f"{top}/{name}")
            info.mode, info.uid, info.gid, info.uname, info.gname = mode, 0, 0, "", ""
            with open(src, "rb") as f:
                tar.addfile(info, f)


def write_checksum(path: Path) -> str:
    with open(path, "rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n",
                                                     encoding="utf-8", newline="\n")
    return digest


def make_archive(app: Path, layout: Layout, version: str, tag: str) -> Path:
    layout.release.mkdir(parents=True, exist_ok=True)
    stem = f"{APP}-{version}-{tag}"
    kind = os_name()
    if kind == "windows":
        dest = layout.release / f"{stem}.zip"
        zip_windows(app, dest)
    elif kind == "macos":
        dest = layout.release / f"{stem}.zip"
        zip_macos(app, dest)
    else:
        dest = layout.release / f"{stem}.tar.gz"
        tar_linux(app, dest, stem)
    return dest


# ---- command line ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="build.py",
        description="Build the standalone RawJpgImport app (PyInstaller), archive it and "
                    "optionally smoke-test it.")
    p.add_argument("--out", metavar="DIR",
                   help=f"build folder (default: ${BUILD_DIR_ENV}, else the repository): the "
                        "app goes to DIR/dist, archives to DIR/dist/release, temporary files "
                        "to DIR/build")
    p.add_argument("--platform-tag", metavar="TAG",
                   help="suffix of the archive name, e.g. windows-x64, macos-arm64, macos-x64, "
                        f"linux-x64 (default for this machine: {_safe_default_tag()})")
    p.add_argument("--smoke", action="store_true",
                   help="run the built app with --selftest and fail unless it exits 0")
    p.add_argument("--skip-build", action="store_true",
                   help="reuse the app already in the dist folder (smoke-test and archive only)")
    p.add_argument("--no-archive", action="store_true", help="do not write the release archive")
    return p.parse_args(argv)


def _safe_default_tag() -> str:
    try:
        return default_platform_tag()
    except BuildError:
        return "unknown"


def run(args: argparse.Namespace) -> None:
    layout = make_layout(args.out)
    version = read_version()
    tag = args.platform_tag or default_platform_tag()
    if not TAG_RE.match(tag):
        raise BuildError(f"bad --platform-tag {tag!r}: use lowercase letters, digits, '.', "
                         "'-' or '_'")
    print(f"{APP} {version} ({tag})\n  app:      {layout.dist}\n  work:     {layout.work}\n"
          f"  archives: {layout.release}", flush=True)
    warn_if_synced(layout)
    if not args.skip_build:
        require_pyinstaller()
        run_pyinstaller(layout)
    app = built_app(layout)
    if not app.exists():
        raise BuildError(f"no built app at {app}" + (" (run without --skip-build)"
                                                     if args.skip_build else ""))
    if args.smoke:
        smoke_test(app_executable(app))
    if not args.no_archive:
        archive = make_archive(app, layout, version, tag)
        digest = write_checksum(archive)
        print(f"archive: {archive}\nsha256:  {digest}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
