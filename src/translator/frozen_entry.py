"""Entry point used by the packaged build.

Kept in the package rather than in ``packaging/launcher.py`` for one reason: it
has to be testable. The previous version lived only in the PyInstaller launcher
and called ``main()`` with no arguments -- frozen builds have an empty
``sys.argv[1:]``, so argparse found no subcommand, printed help and returned 1.
The app exited before creating a window: "double-click, nothing happens".

That class of bug is invisible from the outside, so this module is importable and
covered by a test that asserts what argv the frozen entry actually passes on.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback

log = logging.getLogger(__name__)

__all__ = ["entry", "DEFAULT_ARGV"]

DEFAULT_ARGV = ["run"]
"""What a double-clicked executable should do: start the tray application."""


def _log_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Translator", "logs")


def note(text: str) -> None:
    """Append to startup.log, falling back to a temp dir. Never raises."""
    candidates = [_log_dir(), os.path.join(os.environ.get("TEMP", "/tmp"), "Translator")]
    for directory in candidates:
        try:
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "startup.log"), "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
            return
        except Exception:  # noqa: BLE001
            continue


def entry(argv: list[str] | None = None) -> int:
    """Run the packaged application. Returns the process exit code."""
    from .__main__ import main

    note(f"--- launcher start: {sys.executable} ---")
    try:
        code = int(main(list(DEFAULT_ARGV) if argv is None else argv))
    except SystemExit:
        raise
    except BaseException:
        detail = traceback.format_exc()
        note(detail)
        try:
            from .runtime.report import report_fatal

            report_fatal("程序启动失败", detail)
        except Exception:  # noqa: BLE001
            pass
        return 3

    note(f"--- exit {code} ---")
    if code != 0:
        # A non-zero exit without a window is the "nothing happens" case. Make it
        # say so instead of leaving a single number in a log file.
        try:
            from .runtime.report import report_fatal

            report_fatal(f"启动未完成（退出码 {code}）", _log_tail())
        except Exception:  # noqa: BLE001
            pass
    return code


def _log_tail(limit: int = 40) -> str:
    try:
        path = os.path.join(_log_dir(), "translator.log")
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return "".join(fh.readlines()[-limit:])
    except Exception:  # noqa: BLE001
        return "(no log)"
