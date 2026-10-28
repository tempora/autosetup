"""Entry point of the frozen server the desktop app bundles (autosetup-server.exe)."""

import sys

from autosetup.cli import main

if __name__ == "__main__":
    sys.exit(main())
