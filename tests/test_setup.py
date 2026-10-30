from __future__ import annotations

import pytest

from translator.core.config import AppConfig
from translator.runtime import setup as setup_mod
from translator.runtime.setup import run_setup


@pytest.fixture
def cfg():
    c = AppConfig()
    c.translate.api_key = "sk-test"
    return c


def _no_regions(c: AppConfig) -> None:
    c.screen.region = None
    c.screen.regions = []


# ------------------------------------------------------------------ happy path


def test_existing_key_and_regions_need_no_prompt(cfg, capsys):
    cfg.screen.regions = [(0.0, 0.6, 0.3, 0.2)]
    result = run_setup(cfg, interactive=False)
    assert result.ok
    out = capsys.readouterr().out
    assert "密钥已就绪" in out
    assert "已配置 1 个扫描区域" in out
    assert result.changed is False


def test_local_endpoint_needs_no_key(cfg):
    cfg.translate.api_key = ""
    cfg.translate.base_url = "http://localhost:11434/v1"
    cfg.screen.regions = [(0.0, 0.6, 0.3, 0.2)]
    assert run_setup(cfg, interactive=False).ok


# ---------------------------------------------------------------------- steps


def test_missing_key_without_interaction_fails(cfg):
    cfg.translate.api_key = ""
    cfg.screen.regions = [(0.0, 0.6, 0.3, 0.2)]
    result = run_setup(cfg, interactive=False)
    assert not result.ok
    assert "密钥" in result.message


def test_key_prompt_is_delegated_to_credentials(cfg, monkeypatch, capsys):
    cfg.translate.api_key = ""
    cfg.screen.regions = [(0.0, 0.6, 0.3, 0.2)]
    called = {}

    def fake_ensure(translate_cfg, interactive=True, reason=""):
        called["interactive"] = interactive
        return "sk-new"

    monkeypatch.setattr(setup_mod, "ensure_credentials", fake_ensure)
    assert run_setup(cfg, interactive=True).ok
    assert called["interactive"] is True


def test_key_prompt_failure_is_reported(cfg, monkeypatch):
    cfg.translate.api_key = ""
    cfg.screen.regions = [(0.0, 0.6, 0.3, 0.2)]

    def boom(*args, **kwargs):
        raise setup_mod.MissingApiKey("拒绝")

    monkeypatch.setattr(setup_mod, "ensure_credentials", boom)
    result = run_setup(cfg, interactive=True)
    assert not result.ok
    assert "拒绝" in result.message


def test_regions_are_picked_and_saved(cfg, monkeypatch, tmp_path, capsys):
    from translator.ui.roi_picker import PickedRegion

    _no_regions(cfg)
    picked = [
        PickedRegion(2, (0, 600, 300, 200), (0.0, 0.55, 0.3, 0.19), (1920, 1080)),
        PickedRegion(2, (1300, 50, 400, 200), (0.68, 0.05, 0.21, 0.19), (1920, 1080)),
    ]
    monkeypatch.setattr("translator.ui.roi_picker.pick_regions", lambda monitor=1: picked)

    target = tmp_path / "config.toml"
    result = run_setup(cfg, interactive=True, config_path=str(target))

    assert result.ok
    assert result.changed is True
    assert cfg.screen.regions == [r.fractions for r in picked]
    assert cfg.screen.monitor == 2
    assert target.exists()

    from translator.core.config import AppConfig as AC

    assert len(AC.load(target).screen.regions) == 2


def test_skipping_the_picker_falls_back_to_full_monitor(cfg, monkeypatch, capsys):
    _no_regions(cfg)
    monkeypatch.setattr("translator.ui.roi_picker.pick_regions", lambda monitor=1: None)

    result = run_setup(cfg, interactive=True)
    assert result.ok
    assert cfg.screen.regions == []
    assert cfg.screen.resolved_regions() == [None]
    assert "整个显示器" in capsys.readouterr().out


def test_region_step_is_skipped_when_not_interactive(cfg, capsys):
    _no_regions(cfg)
    result = run_setup(cfg, interactive=False)
    assert result.ok
    assert "整个显示器" in capsys.readouterr().out


def test_legacy_single_region_counts_as_configured(cfg, capsys):
    cfg.screen.regions = []
    cfg.screen.region = (0.0, 0.6, 0.3, 0.2)
    result = run_setup(cfg, interactive=False)
    assert result.ok
    assert "已配置 1 个扫描区域" in capsys.readouterr().out


def test_default_config_path_is_isolated_during_tests():
    """Regression guard: a test run once overwrote the developer's real
    config.toml, because run_setup(config_path=None) falls back to the default
    path. The autouse fixture must keep that path inside the pytest tmp tree."""
    from translator.core.config import default_config_path

    path = default_config_path()
    assert "pytest" in str(path), f"tests are using the real config path: {path}"


def test_setup_writes_only_where_it_is_told(cfg, monkeypatch, tmp_path):
    _no_regions(cfg)
    monkeypatch.setattr("translator.ui.roi_picker.pick_regions", lambda monitor=1: None)

    target = tmp_path / "explicit.toml"
    run_setup(cfg, interactive=True, config_path=str(target))
    assert target.exists()
