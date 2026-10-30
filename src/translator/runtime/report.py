"""Make startup failures visible in a windowed build.

A ``console=False`` build has no stderr, so an exception during startup produces
exactly nothing: the user double-clicks, a process appears for a moment and
vanishes, and there is no way to tell why. That is the worst possible failure
mode to ship.

Everything here is best-effort and must never raise: it runs while the process is
already in trouble.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import traceback

log = logging.getLogger(__name__)

__all__ = ["report_fatal", "install_excepthook"]


def _log_path() -> str:
    try:
        from ..core.config import app_data_dir

        return str(app_data_dir() / "logs" / "translator.log")
    except Exception:  # noqa: BLE001
        return "(unknown)"


def _write_log(text: str) -> None:
    with contextlib.suppress(Exception):
        from ..core.config import app_data_dir

        path = app_data_dir() / "logs" / "translator.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def report_fatal(message: str, detail: str = "") -> None:
    """Log the failure and show it in a dialog. Never raises."""
    body = message if not detail else f"{message}\n\n{detail}"
    with contextlib.suppress(Exception):
        log.error("fatal: %s", body)
    with contextlib.suppress(Exception):
        _write_log(f"=== fatal ===\n{body}\n")

    with contextlib.suppress(Exception):
        if _show_dialog(message, detail):
            return

    # Fall back to the Win32 dialog, which needs no toolkit at all.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                f"{body}\n\n日志：{_log_path()}",
                "Translator 启动失败",
                0x10,  # MB_ICONERROR
            )
            return
        except Exception:  # noqa: BLE001
            pass

    with contextlib.suppress(Exception):
        print(body, file=sys.stderr)


def _show_dialog(message: str, detail: str) -> bool:
    """Show a modal error box. Returns True when one was displayed.

    Split out as a seam: ``report_fatal`` runs on the failure path, and a modal
    dialog there is exactly what a test can never get past -- it blocks until
    somebody clicks OK.
    """
    if os.environ.get("TRANSLATOR_NO_DIALOG"):
        return False
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        QApplication.instance() or QApplication([])
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Translator 启动失败")
        box.setText(message)
        if detail:
            box.setDetailedText(f"{detail}\n\n日志：{_log_path()}")
        box.exec()
        return True
    except Exception:  # noqa: BLE001
        return False


def install_excepthook() -> None:
    """Route uncaught exceptions on the main thread to the dialog."""

    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        report_fatal(f"未处理的错误：{exc_value}", text)

    sys.excepthook = hook
