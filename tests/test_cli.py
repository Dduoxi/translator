from __future__ import annotations

import argparse

import pytest

from fpst import __main__ as cli
from fpst.ui.roi_picker import PickedRegion


def _region() -> PickedRegion:
    return PickedRegion(
        monitor=1,
        rect_px=(0, 670, 691, 173),
        fractions=(0.0, 0.62, 0.36, 0.16),
        monitor_size=(1920, 1080),
    )


def _args(**kwargs) -> argparse.Namespace:
    base = {"config": None, "monitor": 1, "verify": False, "write": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_roi_prints_paste_ready_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("fpst.ui.roi_picker.pick_regions", lambda monitor=1: [_region()])
    cfg_path = tmp_path / "config.toml"

    code = cli.cmd_roi(_args(config=str(cfg_path)))
    out = capsys.readouterr().out

    assert code == 0
    assert "regions = [[0.000, 0.620, 0.360, 0.160]]" in out
    assert "fpst run --region 0.000 0.620 0.360 0.160" in out


def test_roi_cancelled_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("fpst.ui.roi_picker.pick_regions", lambda monitor=1: None)
    assert cli.cmd_roi(_args()) == 1
    assert "cancelled" in capsys.readouterr().out


def test_roi_write_persists_the_region(monkeypatch, tmp_path):
    from fpst.core.config import AppConfig

    monkeypatch.setattr("fpst.ui.roi_picker.pick_regions", lambda monitor=1: [_region()])
    cfg_path = tmp_path / "config.toml"

    assert cli.cmd_roi(_args(config=str(cfg_path), write=True)) == 0

    saved = AppConfig.load(cfg_path)
    assert saved.screen.regions == [pytest.approx((0.0, 0.62, 0.36, 0.16))]
    assert saved.screen.region is None
    assert saved.screen.monitor == 1


@pytest.mark.slow
def test_roi_verify_runs_ocr_on_the_selection(monkeypatch, capsys, tmp_path):
    """End to end: pick -> capture that ROI -> OCR -> print what it saw."""
    pytest.importorskip("rapidocr_onnxruntime")
    from PIL import Image

    from fpst.capture.mss_source import MssSource

    # Figure out where the current screen is, then OCR a slice of it.
    source = MssSource(monitor=1, region=(0.0, 0.0, 1.0, 0.2))
    try:
        frame = source.grab()
    finally:
        source.close()
    if frame is None or not frame.image.any():
        pytest.skip("nothing on screen to recognise")

    Image.fromarray(frame.image[:, :, ::-1]).save(tmp_path / "unused.png")  # sanity

    monkeypatch.setattr("fpst.ui.roi_picker.pick_regions", lambda monitor=1: [_region()])
    # Point the mock at a strip of the real screen so OCR has something to do.
    code = cli.cmd_roi(_args(config=None, verify=True))
    out = capsys.readouterr().out
    assert code == 0
    assert "boxes ->" in out


# ------------------------------------------------------ release gate: api key


def _watch_args(**kwargs) -> argparse.Namespace:
    base = {"config": None, "region": None, "fps": None, "no_prompt": True}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_watch_refuses_to_start_without_a_key(monkeypatch, capsys):
    """Release rule: no key, no service -- and fail before touching the screen."""
    from fpst.runtime.credentials import MissingApiKey

    def no_key(*args, **kwargs):
        raise MissingApiKey("未找到翻译密钥")

    monkeypatch.setattr("fpst.runtime.credentials.ensure_api_key", no_key)

    started = []
    monkeypatch.setattr("fpst.capture.make_screen_source", lambda cfg: started.append(True))

    code = cli.cmd_watch(_watch_args())
    captured = capsys.readouterr()

    assert code == 2
    assert "无法启动" in captured.err
    assert started == [], "must not open the screen source before the key is verified"


def test_watch_passes_no_prompt_flag_through(monkeypatch):
    seen = {}

    def fake_ensure(cfg, interactive=True, reason=""):
        seen["interactive"] = interactive
        return "sk-x"

    monkeypatch.setattr("fpst.runtime.credentials.ensure_api_key", fake_ensure)
    monkeypatch.setattr("fpst.pipeline.ScreenPipeline.run_forever", lambda self: None)
    monkeypatch.setattr("fpst.pipeline.ScreenPipeline.stop", lambda self, timeout=3.0: None)
    monkeypatch.setattr("fpst.capture.make_screen_source", lambda cfg: None)

    cli.cmd_watch(_watch_args(no_prompt=True))
    assert seen["interactive"] is False

    cli.cmd_watch(_watch_args(no_prompt=False))
    assert seen["interactive"] is True


def test_run_parser_exposes_every_flag_cmd_run_reads():
    """Regression: cmd_run read args.no_settings while the parser still defined
    --settings, so `fpst run` crashed on startup with AttributeError."""
    parser = cli.build_parser()
    for argv, expected in (
        (["run"], {"no_settings": False, "skip_setup": False, "no_prompt": False}),
        (["run", "--no-settings"], {"no_settings": True}),
        (["run", "--skip-setup", "--no-prompt"], {"skip_setup": True, "no_prompt": True}),
    ):
        args = parser.parse_args(argv)
        for key, value in expected.items():
            assert getattr(args, key) == value, f"{argv} -> {key}"


def test_pkg_version_falls_back_when_metadata_is_gone(monkeypatch, capsys):
    """In a frozen build dist metadata is absent, and doctor used to report every
    dependency as missing -- the one command meant for support, lying."""
    from fpst import __main__ as m

    monkeypatch.setattr(
        m.metadata,
        "version",
        lambda dist: (_ for _ in ()).throw(m.metadata.PackageNotFoundError(dist)),
    )
    # rapidocr is installed in the dev venv, so the import fallback must find it.
    assert m._pkg_version("rapidocr-onnxruntime") == "bundled"
    assert m._pkg_version("definitely-not-installed-xyz") == "-"
