"""Every clickable control must actually be connected.

Written after a real bug: the settings window's 设置密钥 and 重新框选 buttons were
connected to a handler that was captured as ``None`` at construction time, so
clicking them did nothing at all -- no error, no dialog, no log.

Catching that by eye does not scale, so this walks every QPushButton / QAction in
every window and asserts a receiver exists. Qt exposes that through
``QObject.receivers("2clicked(bool)")`` (the "2" prefix is Qt's signal code).
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from translator.core.config import TranslateConfig  # noqa: E402


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


def _connected(obj, signature: str) -> int:
    return obj.receivers(signature)


def _assert_buttons_connected(widget: QWidget, where: str) -> list[str]:
    """Return the labels of buttons with no receiver; also assert none exist."""
    dead: list[str] = []
    for button in widget.findChildren(QPushButton):
        label = button.text().strip() or "<no label>"
        if button.isHidden() and not label:
            continue
        if _connected(button, "2clicked(bool)") == 0:
            dead.append(label)
    assert dead == [], f"{where}: these buttons do nothing when clicked: {dead}"
    return [b.text().strip() for b in widget.findChildren(QPushButton)]


def test_the_audit_actually_detects_a_dead_button(app):
    """A guard against the guard: if this helper cannot see an unconnected button
    it would happily pass while every control in the app is dead."""
    container = QWidget()
    QPushButton("alive", container).clicked.connect(lambda: None)
    QPushButton("dead", container)
    with pytest.raises(AssertionError, match="dead"):
        _assert_buttons_connected(container, "synthetic")


# ------------------------------------------------------------------ settings


def test_every_settings_button_is_connected(app):
    from translator.ui.settings_window import SettingsWindow

    window = SettingsWindow()
    try:
        window.set_handlers(on_pick_regions=lambda: None, on_set_key=lambda: None)
        labels = _assert_buttons_connected(window, "SettingsWindow")
        assert window.regions.remove_button.isEnabled() is False, "nothing selected yet"
        # The two that regressed must actually be there.
        assert "设置密钥…" in labels
        assert "重新框选…" in labels
        assert any("保存" in label for label in labels)
        assert any("取消" in label for label in labels)
    finally:
        window.close()


def test_settings_buttons_work_without_handlers_too(app):
    """Handlers are wired after construction; clicking before that must be a
    no-op rather than an AttributeError."""
    from translator.ui.settings_window import SettingsWindow

    window = SettingsWindow()
    try:
        for button in window.findChildren(QPushButton):
            button.click()  # must not raise
    finally:
        window.close()


# ------------------------------------------------------------- credential box


def test_every_credential_dialog_button_is_connected(app):
    from translator.ui.key_dialog import CredentialDialog

    dialog = CredentialDialog(TranslateConfig())
    try:
        labels = _assert_buttons_connected(dialog, "CredentialDialog")
        assert any("测试" in label for label in labels)
        assert any("保存" in label for label in labels)
        assert any("取消" in label for label in labels)
    finally:
        dialog.close()


# --------------------------------------------------------------------- tray


def test_every_tray_action_is_connected(app):
    """A tray menu item with no receiver is the same bug in a different place."""
    from translator.ui.tray import TrayIcon

    calls: list[str] = []
    tray = TrayIcon(
        on_toggle=lambda: calls.append("toggle"),
        on_settings=lambda: calls.append("settings"),
        on_pick_regions=lambda: calls.append("pick"),
        on_set_key=lambda: calls.append("key"),
        on_quit=lambda: calls.append("quit"),
    )
    try:
        menu = tray._menu
        dead = [
            action.text()
            for action in menu.findChildren(QAction)
            if action.text()
            and not action.isSeparator()
            and _connected(action, "2triggered(bool)") == 0
        ]
        assert dead == [], f"tray actions that do nothing: {dead}"

        # And they really fire.
        for action in menu.findChildren(QAction):
            if action.text() and not action.isSeparator():
                action.trigger()
        assert {"toggle", "settings", "pick", "key", "quit"} <= set(calls)
    finally:
        tray.hide()


# ---------------------------------------------------------------- the app


def test_app_wires_the_settings_handlers(app, tmp_path, monkeypatch):
    """The wiring happens in run_app; assert it is not forgotten there."""
    import inspect

    from translator.ui import app as app_module

    source = inspect.getsource(app_module.run_app)
    assert "set_handlers(" in source, "run_app must connect the settings buttons"
    assert "on_set_key=set_key" in source
    assert "on_pick_regions=pick_regions" in source
