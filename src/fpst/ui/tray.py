"""Tray icon: the control surface you actually use while a game is running.

Full-screen games make a settings dialog unusable, so start/stop, pause and
"re-pick the region" must be reachable from the tray. The icon colour doubles as
the status readout.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .controller import RunState

log = logging.getLogger(__name__)

__all__ = ["TrayIcon"]

STATE_COLOURS = {
    RunState.RUNNING: "#3ddc84",  # green
    RunState.PAUSED: "#ffcc44",  # amber
    RunState.STOPPED: "#8a8f98",  # grey
    RunState.ERROR: "#ff4d4f",  # red
}


def make_icon(colour: str) -> QIcon:
    """A filled circle with a small white 'speech' notch -- readable at 16 px."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(colour))
    painter.drawEllipse(4, 4, 56, 56)
    painter.setBrush(QColor(255, 255, 255, 235))
    painter.drawRoundedRect(18, 22, 28, 8, 4, 4)
    painter.drawRoundedRect(18, 34, 18, 8, 4, 4)
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QObject):
    def __init__(
        self,
        *,
        on_toggle: Callable[[], None],
        on_settings: Callable[[], None],
        on_pick_regions: Callable[[], None],
        on_set_key: Callable[[], None],
        on_quit: Callable[[], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(make_icon(STATE_COLOURS[RunState.STOPPED]), self)
        self._tray.setToolTip("FPS 翻译")
        self._state = RunState.STOPPED

        menu = QMenu()
        self._toggle_action = QAction("暂停", menu)
        self._toggle_action.triggered.connect(on_toggle)
        menu.addAction(self._toggle_action)
        menu.addSeparator()

        for label, handler in (
            ("重新框选区域…", on_pick_regions),
            ("设置…", on_settings),
            ("设置密钥…", on_set_key),
        ):
            action = QAction(label, menu)
            action.triggered.connect(handler)
            menu.addAction(action)

        menu.addSeparator()
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(on_quit)
        menu.addAction(quit_action)

        self._menu = menu  # keep a reference: QSystemTrayIcon does not own it
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self._on_settings = on_settings
        self._tray.show()

    # ------------------------------------------------------------------ state
    def set_state(self, state: str) -> None:
        self._state = state
        self._tray.setIcon(make_icon(STATE_COLOURS.get(state, STATE_COLOURS[RunState.STOPPED])))
        self._toggle_action.setText("暂停" if state == RunState.RUNNING else "启动")

    def set_status(self, text: str) -> None:
        self._tray.setToolTip(f"FPS 翻译\n{text}")

    def notify(self, title: str, message: str) -> None:
        try:
            self._tray.showMessage(title, message, self._tray.icon(), 4000)
        except Exception as exc:  # noqa: BLE001 - not available on every desktop
            log.debug("tray notification failed: %s", exc)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_settings()

    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def hide(self) -> None:
        self._tray.hide()
