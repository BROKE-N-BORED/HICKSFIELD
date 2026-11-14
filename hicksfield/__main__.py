"""Entry point for HicksField.

The Qt GUI is imported lazily so `--version` works without PySide6.
"""

import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-v" in argv:
        from hicksfield import __version__

        print(f"HicksField {__version__}")
        return 0
    if "--help" in argv or "-h" in argv:
        print("HicksField: a free local film studio for Linux.")
        print()
        print("Usage: hicksfield [--version] [--help]")
        print()
        print("With no arguments, opens the HicksField window.")
        return 0
    from hicksfield.gui import run_app

    return run_app(argv)


if __name__ == "__main__":
    sys.exit(main())
