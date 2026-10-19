"""``python -m raw_jpg_import``: no arguments opens the GUI, any arguments run the CLI."""

import sys


def main() -> int:
    if len(sys.argv) <= 1:
        from raw_jpg_import.gui.app import main as gui_main

        return gui_main()
    from raw_jpg_import.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
