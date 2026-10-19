"""Entry point of the frozen app (PyInstaller): opens the window, or runs ``--selftest``.

``RawJpgImport --selftest`` exits 0 when the bundle is complete (see raw_jpg_import.selftest).
A windowed build has no stderr on Windows, so the self-test messages go to the file named by
``RJI_SELFTEST_LOG`` when it is set; ``packaging/build.py --smoke`` uses that to show failures.
"""

from __future__ import annotations

import os
import sys
import traceback


def _selftest() -> int:
    log_path = os.environ.get("RJI_SELFTEST_LOG")
    saved = sys.stdout, sys.stderr
    log = open(log_path, "w", encoding="utf-8") if log_path else None  # noqa: SIM115
    if log is not None:
        sys.stdout = sys.stderr = log
    try:
        from raw_jpg_import.selftest import run

        return run()
    except Exception:
        # Never let it escape: a windowed build would show an error dialog and hang CI.
        traceback.print_exc()
        return 1
    finally:
        if log is not None:
            sys.stdout, sys.stderr = saved
            log.close()


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    from raw_jpg_import.gui.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
