"""PyInstaller entry point.

All the logic lives in translator.frozen_entry so it can be unit-tested; this file
only has to do the two things that are specific to a frozen process.
"""

from __future__ import annotations

import multiprocessing
import sys

from translator.frozen_entry import entry

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(entry())
