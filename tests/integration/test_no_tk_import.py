"""The CLI and the engine must work without Tk (headless servers, minimal Pythons)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import raw_jpg_import

SRC_DIR = str(Path(raw_jpg_import.__file__).resolve().parents[1])


def test_cli_and_engine_do_not_import_tkinter(tmp_path):
    code = ("import raw_jpg_import.cli, raw_jpg_import.engine, raw_jpg_import.report; "
            "import sys; assert 'tkinter' not in sys.modules, 'tkinter was imported'; "
            "print('ok')")
    env = {**os.environ, "PYTHONPATH": SRC_DIR, "RAW_JPG_IMPORT_HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_cli_help_runs_without_tkinter(tmp_path):
    code = ("import sys; sys.modules['tkinter'] = None; "    # any import of tkinter now fails
            "from raw_jpg_import.cli import main; "
            "sys.exit(main(['config', 'path']))")
    env = {**os.environ, "PYTHONPATH": SRC_DIR, "RAW_JPG_IMPORT_HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "config.toml" in proc.stdout
