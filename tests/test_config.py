from fpst.core.config import AppConfig, ScreenConfig


def test_defaults_load_when_missing(tmp_path):
    cfg = AppConfig.load(tmp_path / "nope.toml")
    assert cfg.screen.engine == "auto"
    assert cfg.translate.dst_lang == "zh"


def test_partial_toml_overrides_only_given_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        """
[screen]
monitor = 2
region = [0.0, 0.6, 1.0, 0.4]

[translate]
engine = "openai"
""",
        encoding="utf-8",
    )
    cfg = AppConfig.load(p)
    assert cfg.screen.monitor == 2
    assert cfg.screen.region == (0.0, 0.6, 1.0, 0.4)  # list -> tuple
    assert cfg.screen.max_fps == 2.0  # untouched default
    assert cfg.translate.engine == "openai"


def test_unknown_keys_are_ignored(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[screen]\nbogus = 1\n\n[nosuchsection]\nx = 1\n", encoding="utf-8")
    cfg = AppConfig.load(p)
    assert isinstance(cfg.screen, ScreenConfig)


def test_save_load_roundtrip(tmp_path):
    cfg = AppConfig()
    cfg.screen.region = (0.1, 0.2, 0.3, 0.4)
    cfg.ui.font_size = 28
    cfg.translate.glossary = {"gg": "打得好"}
    out = cfg.save(tmp_path / "config.toml")
    back = AppConfig.load(out)
    assert back.screen.region == (0.1, 0.2, 0.3, 0.4)
    assert back.ui.font_size == 28
    assert back.translate.glossary == {"gg": "打得好"}


def test_glossary_keys_with_spaces_survive_a_roundtrip(tmp_path):
    """Bare TOML keys cannot contain spaces; "cover me" must be quoted."""
    cfg = AppConfig()
    cfg.translate.glossary = {"cover me": "掩护我", "on my mark": "听我口令", "gg": "打得好"}
    out = cfg.save(tmp_path / "config.toml")

    text = out.read_text(encoding="utf-8")
    assert '"cover me" = "掩护我"' in text

    back = AppConfig.load(out)
    assert back.translate.glossary == cfg.translate.glossary


def test_default_config_roundtrips():
    """The shipped defaults must be able to survive a save/load cycle."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = AppConfig().save(Path(tmp) / "config.toml")
        back = AppConfig.load(out)
    assert back.translate.glossary == AppConfig().translate.glossary
