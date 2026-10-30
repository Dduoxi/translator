from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from translator.core.config import AppConfig  # noqa: E402
from translator.core.types import Lang, Source, Subtitle  # noqa: E402


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def cfg_path(tmp_path):
    cfg = AppConfig()
    cfg.translate.api_key = "sk-test"
    cfg.screen.regions = [(0.0, 0.6, 0.3, 0.2)]
    path = tmp_path / "config.toml"
    cfg.save(path)
    return path


# ------------------------------------------------------------------ controller


def test_controller_loads_config_from_disk(app, cfg_path):
    from translator.ui.controller import AppController

    controller = AppController(cfg_path)
    try:
        assert controller.cfg.screen.regions == [(0.0, 0.6, 0.3, 0.2)]
        assert controller.state == "stopped"
    finally:
        controller.shutdown()


def test_controller_counts_subtitles(app, cfg_path):
    from translator.ui.controller import AppController

    controller = AppController(cfg_path)
    try:
        seen = []
        controller.subtitle_seen.connect(lambda: seen.append(1))
        controller.bus.publish(Subtitle("a", "b", Source.SCREEN, Lang.EN))
        controller.bus.publish(Subtitle("c", "d", Source.VOICE, Lang.EN))
        assert controller.subtitle_count == 2
        assert len(seen) == 2
    finally:
        controller.shutdown()


def test_snapshot_reports_core_fields(app, cfg_path):
    from translator.ui.controller import AppController

    controller = AppController(cfg_path)
    try:
        snap = controller.snapshot()
        assert snap["state"] == "stopped"
        assert snap["regions"] == 1
        assert snap["model"] == controller.cfg.translate.model
        assert snap["subtitles"] == 0
    finally:
        controller.shutdown()


def test_status_text_distinguishes_states(app, cfg_path):
    from translator.ui.controller import AppController, RunState

    controller = AppController(cfg_path)
    try:
        assert "停止" in controller.status_text()
        controller._set_state(RunState.RUNNING)
        assert "运行中" in controller.status_text()
        controller._set_state(RunState.ERROR)
        assert "错误" in controller.status_text()
    finally:
        controller.shutdown()


def test_pause_and_resume_are_idempotent(app, cfg_path):
    from translator.ui.controller import AppController

    controller = AppController(cfg_path)
    try:
        controller.pause()  # nothing running
        assert controller.state in {"paused", "stopped"}
        controller.stop()
        controller.stop()
        assert controller.state == "stopped"
    finally:
        controller.shutdown()


def test_save_config_writes_the_file(app, cfg_path):
    from translator.ui.controller import AppController

    controller = AppController(cfg_path)
    try:
        controller.cfg.ui.font_size = 33
        controller.save_config()
        assert AppConfig.load(cfg_path).ui.font_size == 33
    finally:
        controller.shutdown()


def test_start_failure_becomes_an_error_state(app, cfg_path, monkeypatch):
    """A broken engine must surface in the UI, not crash the tray."""
    from translator.ui import controller as mod

    def boom(*args, **kwargs):
        raise RuntimeError("no screen")

    monkeypatch.setattr(mod.ScreenPipeline, "from_config", boom)
    controller = mod.AppController(cfg_path)
    try:
        errors = []
        controller.error.connect(errors.append)
        assert controller.start() is False
        assert controller.state == "error"
        assert "no screen" in errors[0]
    finally:
        controller.shutdown()


# ------------------------------------------------------------------ settings


def test_settings_roundtrip_preserves_config(app, cfg_path):
    from translator.ui.settings_window import SettingsWindow

    cfg = AppConfig.load(cfg_path)
    window = SettingsWindow()
    try:
        window.load_config(cfg)
        out = window.collect_config()
        assert out.screen.regions == cfg.screen.regions
        assert out.ui.font_size == cfg.ui.font_size
        assert out.translate.model == cfg.translate.model
        assert out.translate.base_url == cfg.translate.base_url
    finally:
        window.close()


def test_settings_edits_are_applied(app, cfg_path):
    from translator.ui.settings_window import SettingsWindow

    window = SettingsWindow()
    try:
        window.load_config(AppConfig.load(cfg_path))
        window.appearance.font_size.setValue(30)
        window.appearance.anchor.setCurrentIndex(window.appearance.anchor.findData("bottom-center"))
        window.performance.max_fps.setValue(5.0)
        out = window.collect_config()
        assert out.ui.font_size == 30
        assert out.ui.anchor == "bottom-center"
        assert out.screen.max_fps == 5.0
    finally:
        window.close()


def test_settings_cancel_does_not_touch_the_source_config(app, cfg_path):
    """The window edits a copy, so a rejected dialog cannot corrupt anything."""
    from translator.ui.settings_window import SettingsWindow

    original = AppConfig.load(cfg_path)
    window = SettingsWindow()
    try:
        window.load_config(original)
        window.appearance.font_size.setValue(48)
        window.reject()  # Cancel
        assert original.ui.font_size != 48
        assert AppConfig.load(cfg_path).ui.font_size != 48
    finally:
        window.close()


def test_regions_tab_edits_and_removes(app, cfg_path):
    from translator.ui.settings_window import SettingsWindow

    cfg = AppConfig.load(cfg_path)
    cfg.screen.regions = [(0.0, 0.6, 0.3, 0.2), (0.7, 0.05, 0.25, 0.2)]
    window = SettingsWindow()
    try:
        window.load_config(cfg)
        assert window.regions.table.rowCount() == 2
        window.regions.table.selectRow(0)
        window.regions._remove()
        out = window.collect_config()
        assert len(out.screen.regions) == 1
        assert out.screen.regions[0] == pytest.approx((0.7, 0.05, 0.25, 0.2))
    finally:
        window.close()


def test_status_tab_renders_a_snapshot(app, cfg_path):
    from translator.ui.settings_window import SettingsWindow

    window = SettingsWindow()
    try:
        window.update_status(
            {
                "state": "running",
                "subtitles": 7,
                "idle_for": 2.0,
                "regions": 2,
                "model": "deepseek-flash",
                "ocr_ms": 240.0,
                "translate_ms": 900.0,
                "cache": {"hits": 3, "misses": 4, "glossary": 1, "hit_rate": 0.43},
            }
        )
        text = window.status.summary.text()
        assert "已翻译：7" in text
        assert "命中率" in text
    finally:
        window.close()


def test_glossary_edit_replaces_both_dicts(app, cfg_path):
    from translator.ui.settings_window import SettingsWindow

    window = SettingsWindow()
    try:
        window.load_config(AppConfig.load(cfg_path))
        window.translate.glossary.insertRow(0)
        from PySide6.QtWidgets import QTableWidgetItem

        window.translate.glossary.setItem(0, 0, QTableWidgetItem("tp"))
        window.translate.glossary.setItem(0, 1, QTableWidgetItem("传送"))
        out = window.collect_config()
        assert out.translate.glossary.get("tp") == "传送"
        assert out.translate.glossary_extra == {}
    finally:
        window.close()


# ----------------------------------------------------------------------- tray


def test_tray_builds_menu_and_tracks_state(app):
    from translator.ui.tray import TrayIcon

    calls = []
    tray = TrayIcon(
        on_toggle=lambda: calls.append("toggle"),
        on_settings=lambda: calls.append("settings"),
        on_pick_regions=lambda: calls.append("pick"),
        on_set_key=lambda: calls.append("key"),
        on_quit=lambda: calls.append("quit"),
    )
    try:
        labels = [a.text() for a in tray._menu.actions() if a.text()]
        assert "暂停" in labels
        assert "设置…" in labels
        assert "退出" in labels

        tray.set_state("running")
        assert tray._toggle_action.text() == "暂停"
        tray.set_state("stopped")
        assert tray._toggle_action.text() == "启动"

        tray._toggle_action.trigger()
        assert calls == ["toggle"]
    finally:
        tray.hide()


def test_tray_icon_colours_differ_per_state(app):
    from translator.ui.tray import STATE_COLOURS

    assert len({*STATE_COLOURS.values()}) == len(STATE_COLOURS), "each state needs its own colour"


def test_tray_single_click_opens_settings(app):
    """Regression: only DoubleClick was wired, and several desktops never emit
    it, so clicking the tray icon appeared to do nothing."""
    from PySide6.QtWidgets import QSystemTrayIcon

    from translator.ui.tray import TrayIcon

    calls = []
    tray = TrayIcon(
        on_toggle=lambda: calls.append("toggle"),
        on_settings=lambda: calls.append("settings"),
        on_pick_regions=lambda: None,
        on_set_key=lambda: None,
        on_quit=lambda: None,
    )
    try:
        reasons = QSystemTrayIcon.ActivationReason
        tray._on_activated(reasons.Trigger)
        assert calls == ["settings"]
        tray._on_activated(reasons.DoubleClick)
        assert calls == ["settings", "settings"]
        tray._on_activated(reasons.MiddleClick)
        assert calls == ["settings", "settings", "toggle"]
    finally:
        tray.hide()


def test_hidden_window_is_not_offered_as_the_settings_target(app, cfg_path):
    """The settings window must be openable repeatedly, not just once."""
    from translator.ui.settings_window import SettingsWindow

    window = SettingsWindow()
    try:
        window.load_config(AppConfig.load(cfg_path))
        window.show()
        app.processEvents()
        assert window.isVisible()
        window.hide()
        window.show()
        app.processEvents()
        assert window.isVisible(), "window must be re-showable after hiding"
    finally:
        window.close()


def test_overlay_picks_the_screen_by_origin_not_by_index(app):
    """Regression: the overlay used a raw Qt screen index while capture matched by
    absolute origin (mss and Qt order screens differently), so subtitles could
    appear on one display while OCR read another."""
    from translator.ui.overlay import SubtitleOverlay
    from translator.ui.screens import screen_origin

    screens = app.screens()
    if len(screens) < 2:
        pytest.skip("needs two screens")

    target = screens[1]
    origin = screen_origin(target)
    # Deliberately pass the *wrong* index; the origin must win.
    widget = SubtitleOverlay(AppConfig().ui, monitor=1, origin=origin)
    try:
        assert widget.geometry() == target.geometry()
    finally:
        widget.close()


def test_find_screen_falls_back_to_index_when_origin_is_unknown(app):
    from translator.ui.screens import find_screen

    screens = app.screens()
    assert find_screen(None, 0) is screens[0]
    assert find_screen((999999, 999999), 0) is screens[0]


def test_settings_preserve_small_floats_exactly(app, cfg_path):
    """Regression: the spin box had 2 decimals, so min_change_ratio = 0.004 showed
    as 0.00 and every save from the settings window silently disabled the change
    gate -- the single biggest CPU optimisation in the product."""
    from translator.ui.settings_window import SettingsWindow

    cfg = AppConfig.load(cfg_path)
    cfg.screen.min_change_ratio = 0.004
    window = SettingsWindow()
    try:
        window.load_config(cfg)
        out = window.collect_config()
        assert out.screen.min_change_ratio == 0.004
    finally:
        window.close()


@pytest.mark.parametrize("value", [0.004, 0.012, 0.05])
def test_small_ratio_survives_the_spinbox(app, cfg_path, value):
    from translator.ui.settings_window import SettingsWindow

    cfg = AppConfig.load(cfg_path)
    cfg.screen.min_change_ratio = value
    window = SettingsWindow()
    try:
        window.load_config(cfg)
        assert window.performance.min_change.value() == value
        assert window.collect_config().screen.min_change_ratio == value
    finally:
        window.close()


def test_missing_credentials_do_not_abort_startup(app):
    """Regression: a windowed build has no stdin, so an interactive credential
    prompt threw and the process exited before any window appeared -- the app
    looked like it simply would not open. It must start and point at settings."""
    from translator.core.config import TranslateConfig
    from translator.runtime import credentials

    cfg = TranslateConfig()
    cfg.api_key = ""
    assert credentials.can_prompt() in (True, False)  # never raises
    # With no terminal and no stored key the call must report, not crash.
    try:
        credentials.ensure_credentials(cfg, interactive=True)
    except credentials.MissingApiKey as exc:
        assert "设置" in str(exc) or "TRANSLATOR_API_KEY" in str(exc)


def test_can_prompt_is_false_without_stdin(monkeypatch):
    from translator.runtime import credentials

    monkeypatch.setattr(credentials.sys, "stdin", None)
    assert credentials.can_prompt() is False


def test_report_fatal_never_raises(app, monkeypatch, tmp_path):
    """It runs while the process is already broken; raising there would hide the
    original error. The dialog is stubbed out -- a modal box would block the test
    suite forever (it did exactly that before this seam existed)."""
    from translator.runtime import report

    shown = []
    monkeypatch.setattr(report, "_write_log", lambda text: None)
    monkeypatch.setattr(report, "_log_path", lambda: str(tmp_path / "x.log"))
    monkeypatch.setattr(
        report, "_show_dialog", lambda message, detail: shown.append(message) or True
    )
    report.report_fatal("boom", "traceback here")  # must not raise
    assert shown == ["boom"]


def test_report_fatal_survives_a_broken_logger(monkeypatch):
    """Logging itself may be what failed; the reporter must still return."""
    from translator.runtime import report

    monkeypatch.setattr(report, "_write_log", lambda text: (_ for _ in ()).throw(OSError()))

    def boom(*_a, **_k):
        raise OSError("no dialog either")

    monkeypatch.setattr(report, "_show_dialog", boom)
    report.report_fatal("still fine")  # must not raise


def test_dialog_is_suppressed_by_env(monkeypatch):
    from translator.runtime import report

    monkeypatch.setenv("TRANSLATOR_NO_DIALOG", "1")
    assert report._show_dialog("x", "") is False


# ------------------------------------------------ settings buttons must be wired


def test_settings_buttons_do_nothing_until_handlers_are_set(app):
    """The window is created before the application's handlers exist; if the tabs
    captured None at construction, both buttons silently did nothing."""
    from translator.ui.settings_window import SettingsWindow

    window = SettingsWindow()
    try:
        assert window.regions._on_pick is None
        assert window.translate._on_auth is None
        window.regions._pick()  # must not raise
        window.translate._request_key()
    finally:
        window.close()


def test_set_handlers_connects_both_buttons(app):
    from translator.ui.settings_window import SettingsWindow

    calls: list[str] = []
    window = SettingsWindow()
    try:
        window.set_handlers(
            on_pick_regions=lambda: calls.append("pick"),
            on_set_key=lambda: calls.append("key"),
        )
        window.regions._pick()
        window.translate._request_key()
        assert calls == ["pick", "key"]
    finally:
        window.close()


def test_real_button_click_reaches_the_late_handler(app):
    """Drive the actual QPushButton, not just the slot: the bug was that the
    button was connected to a captured None."""
    from PySide6.QtWidgets import QPushButton

    from translator.ui.settings_window import SettingsWindow

    calls: list[str] = []
    window = SettingsWindow()
    try:
        window.set_handlers(on_set_key=lambda: calls.append("key"))
        button = next(
            b for b in window.translate.findChildren(QPushButton) if b.text() == "设置密钥…"
        )
        button.click()
        assert calls == ["key"], "clicking 设置密钥… must open the dialog"
    finally:
        window.close()


# ----------------------------------------------------------- credential dialog


def test_credential_dialog_defaults_to_llm_fields(app):
    from translator.core.config import TranslateConfig
    from translator.ui.key_dialog import PROVIDER_LLM, CredentialDialog

    dialog = CredentialDialog(TranslateConfig())
    try:
        assert dialog._provider() == PROVIDER_LLM
        assert dialog.label_a.text() == "API Key"
        assert "确认" in dialog.label_b.text(), "a typo here is unrecoverable later"
    finally:
        dialog.close()


def test_credential_dialog_switches_to_baidu_fields(app):
    from translator.core.config import TranslateConfig
    from translator.ui.key_dialog import PROVIDER_BAIDU, CredentialDialog

    dialog = CredentialDialog(TranslateConfig())
    try:
        dialog.provider.setCurrentIndex(dialog.provider.findData(PROVIDER_BAIDU))
        assert dialog.label_a.text() == "APPID"
        assert "fanyi.baidu.com" in dialog.endpoint_label.text()
    finally:
        dialog.close()


def test_credential_dialog_rejects_mismatched_key(app):
    from translator.core.config import TranslateConfig
    from translator.ui.key_dialog import CredentialDialog

    dialog = CredentialDialog(TranslateConfig())
    try:
        dialog.field_a.setText("sk-one")
        dialog.field_b.setText("sk-two")
        assert dialog._values() is None
        assert "不一致" in dialog.status.text()
    finally:
        dialog.close()


def test_credential_dialog_requires_a_value(app):
    from translator.core.config import TranslateConfig
    from translator.ui.key_dialog import CredentialDialog

    dialog = CredentialDialog(TranslateConfig())
    try:
        dialog.field_a.setText("   ")
        assert dialog._values() is None
    finally:
        dialog.close()


def test_credential_dialog_saves_after_validation(app, monkeypatch):
    from translator.core.config import TranslateConfig
    from translator.ui import key_dialog

    saved: list[str] = []
    monkeypatch.setattr(key_dialog, "save_api_key", lambda key: (saved.append(key), "keyring")[1])
    monkeypatch.setattr(key_dialog, "validate_api_key", lambda *a, **k: (True, "验证通过"))
    dialog = key_dialog.CredentialDialog(TranslateConfig())
    try:
        dialog.field_a.setText("sk-good")
        dialog.field_b.setText("sk-good")
        dialog._on_save()
        assert saved == ["sk-good"]
        assert dialog.saved is True
    finally:
        dialog.close()


def test_credential_dialog_refuses_to_store_an_unvalidated_key(app, monkeypatch):
    """A typo stored silently only shows up later as "no subtitles", which is
    far harder to diagnose than a rejected dialog."""
    from translator.core.config import TranslateConfig
    from translator.ui import key_dialog

    saved: list[str] = []
    monkeypatch.setattr(key_dialog, "save_api_key", lambda key: (saved.append(key), "keyring")[1])
    monkeypatch.setattr(key_dialog, "validate_api_key", lambda *a, **k: (False, "密钥无效"))
    dialog = key_dialog.CredentialDialog(TranslateConfig())
    try:
        dialog.field_a.setText("sk-bad")
        dialog.field_b.setText("sk-bad")
        dialog._on_save()
        assert saved == [], "must not persist a key that failed validation"
        assert dialog.saved is False
        assert "无效" in dialog.status.text()
    finally:
        dialog.close()


def test_credential_dialog_skips_revalidation_after_a_successful_test(app, monkeypatch):
    from translator.core.config import TranslateConfig
    from translator.ui import key_dialog

    calls: list[int] = []
    monkeypatch.setattr(
        key_dialog, "validate_api_key", lambda *a, **k: (calls.append(1), (True, "ok"))[1]
    )
    monkeypatch.setattr(key_dialog, "save_api_key", lambda key: "keyring")
    dialog = key_dialog.CredentialDialog(TranslateConfig())
    try:
        dialog.field_a.setText("sk-good")
        dialog.field_b.setText("sk-good")
        dialog._on_test()
        dialog._on_save()
        assert len(calls) == 1, "a passing test should not be repeated on save"
    finally:
        dialog.close()
