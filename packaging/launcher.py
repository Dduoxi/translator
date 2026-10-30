"""Entry point for the packaged build.

PyInstaller needs a plain script, not a module path: `python -m fpst` semantics
do not survive freezing on their own.
"""

from __future__ import annotations

import multiprocessing
import sys

from fpst.__main__ import main

if __name__ == "__main__":
    # Required by PyInstaller whenever anything spawns a process.
    multiprocessing.freeze_support()
    sys.exit(main())
