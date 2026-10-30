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
    monkeypatch.setattr("fpst.ui.roi_picker.pick_region", lambda monitor=1: _region())
    cfg_path = tmp_path / "config.toml"

    code = cli.cmd_roi(_args(config=str(cfg_path)))
    out = capsys.readouterr().out

    assert code == 0
    assert "region = [0.000, 0.620, 0.360, 0.160]" in out
    assert "fpst watch --region 0.000 0.620 0.360 0.160" in out


def test_roi_cancelled_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("fpst.ui.roi_picker.pick_region", lambda monitor=1: None)
    assert cli.cmd_roi(_args()) == 1
    assert "cancelled" in capsys.readouterr().out


def test_roi_write_persists_the_region(monkeypatch, tmp_path):
    from fpst.core.config import AppConfig

    monkeypatch.setattr("fpst.ui.roi_picker.pick_region", lambda monitor=1: _region())
    cfg_path = tmp_path / "config.toml"

    assert cli.cmd_roi(_args(config=str(cfg_path), write=True)) == 0

    saved = AppConfig.load(cfg_path)
    assert saved.screen.region == pytest.approx((0.0, 0.62, 0.36, 0.16))
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

    monkeypatch.setattr("fpst.ui.roi_picker.pick_region", lambda monitor=1: _region())
    # Point the mock at a strip of the real screen so OCR has something to do.
    code = cli.cmd_roi(_args(config=None, verify=True))
    out = capsys.readouterr().out
    assert code == 0
    assert "boxes ->" in out
