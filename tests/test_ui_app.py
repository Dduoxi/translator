from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fpst.core.config import AppConfig  # noqa: E402
from fpst.core.types import Lang, Source, Subtitle  # noqa: E402


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
    from fpst.ui.controller import AppController

    controller = AppController(cfg_path)
    try:
        assert controller.cfg.screen.regions == [(0.0, 0.6, 0.3, 0.2)]
        assert controller.state == "stopped"
    finally:
        controller.shutdown()


def test_controller_counts_subtitles(app, cfg_path):
    from fpst.ui.controller import AppController

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
    from fpst.ui.controller import AppController

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
    from fpst.ui.controller import AppController, RunState

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
    from fpst.ui.controller import AppController

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
    from fpst.ui.controller import AppController

    controller = AppController(cfg_path)
    try:
        controller.cfg.ui.font_size = 33
        controller.save_config()
        assert AppConfig.load(cfg_path).ui.font_size == 33
    finally:
        controller.shutdown()


def test_start_failure_becomes_an_error_state(app, cfg_path, monkeypatch):
    """A broken engine must surface in the UI, not crash the tray."""
    from fpst.ui import controller as mod

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
    from fpst.ui.settings_window import SettingsWindow

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
    from fpst.ui.settings_window import SettingsWindow

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
    from fpst.ui.settings_window import SettingsWindow

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
    from fpst.ui.settings_window import SettingsWindow

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
    from fpst.ui.settings_window import SettingsWindow

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
    from fpst.ui.settings_window import SettingsWindow

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
    from fpst.ui.tray import TrayIcon

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
    from fpst.ui.tray import STATE_COLOURS

    assert len({*STATE_COLOURS.values()}) == len(STATE_COLOURS), "each state needs its own colour"
