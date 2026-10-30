"""Entry point for the packaged build.

PyInstaller needs a plain script, not a module path: `python -m translator`
semantics do not survive freezing on their own.

The wrapping here is not defensive padding -- it is the difference between "it
does not open" and an actual error message. A ``console=False`` build has no
stderr, so any exception before the first window appears is completely invisible.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback


def _log_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Translator", "logs")


def _note(text: str) -> None:
    """Append to startup.log, falling back to a temp dir. Never raises."""
    for directory in (_log_dir(), os.path.join(os.environ.get("TEMP", "/tmp"), "Translator")):
        try:
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "startup.log"), "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
            return
        except Exception:  # noqa: BLE001
            continue


def _fatal(message: str, detail: str = "") -> None:
    from translator.runtime.report import report_fatal

    report_fatal(message, detail)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _note(f"--- launcher start: {sys.executable} ---")
    try:
        from translator.__main__ import main
    except BaseException:
        _note(traceback.format_exc())
        _fatal("程序组件加载失败（导入阶段）", traceback.format_exc())
        sys.exit(3)

    try:
        code = int(main())
    except SystemExit:
        raise
    except BaseException:
        _note(traceback.format_exc())
        _fatal("程序运行中崩溃", traceback.format_exc())
        sys.exit(4)

    _note(f"--- exit {code} ---")
    sys.exit(code)
