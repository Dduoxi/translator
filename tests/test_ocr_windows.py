"""Windows system OCR.

The WinRT call cannot run on the build machine, so the WinRT boundary is a small
backend interface and everything above it is tested here with a fake.
"""

from __future__ import annotations

import numpy as np
import pytest

from translator.core.types import Frame, Lang, Source
from translator.ocr.windows_engine import RawLine, WindowsOCREngine, _to_bgra, _upscale


class FakeBackend:
    def __init__(self, lines: list[RawLine] | None = None) -> None:
        self.lines = lines or []
        self.calls: list[tuple[int, int, int, int]] = []
        self.warmed = 0

    def warmup(self) -> None:
        self.warmed += 1

    def recognize(self, bgra: bytes, width: int, height: int) -> list[RawLine]:
        self.calls.append((len(bgra), width, height, bgra[3]))  # 4th = first alpha
        return list(self.lines)


def _frame(w: int = 200, h: int = 60) -> Frame:
    return Frame(image=np.zeros((h, w, 3), np.uint8))


def _engine(backend: FakeBackend, **kwargs) -> WindowsOCREngine:
    return WindowsOCREngine(backend=backend, **kwargs)


# ------------------------------------------------------------------ conversion


def test_bgr_is_converted_to_bgra():
    img = np.zeros((2, 3, 3), np.uint8)
    img[:, :, 0] = 10  # B
    img[:, :, 1] = 20  # G
    img[:, :, 2] = 30  # R
    data = _to_bgra(img)
    assert len(data) == 2 * 3 * 4, "one byte per channel plus alpha"
    assert data[0:4] == bytes([10, 20, 30, 255]), "order must stay B,G,R then opaque alpha"


def test_non_bgr_image_is_rejected():
    with pytest.raises(ValueError):
        _to_bgra(np.zeros((5, 5), np.uint8))


def test_upscale_repeats_pixels():
    img = np.arange(4, dtype=np.uint8).reshape(2, 2)
    out = _upscale(img, 2)
    assert out.shape == (4, 4)
    assert out[0, 0] == img[0, 0] and out[1, 1] == img[0, 0]


def test_upscale_never_shrinks():
    img = np.zeros((2, 2), np.uint8)
    assert _upscale(img, 0.5).shape == (4, 4), "clamped to at least 2x"


# -------------------------------------------------------------------- mapping


def test_lines_become_text_regions():
    backend = FakeBackend(
        [
            RawLine("rush b site", 10, 20, 100, 18),
            RawLine("gg wp", 10, 44, 60, 18),
        ]
    )
    regions = _engine(backend).recognize(_frame())
    assert [r.text for r in regions] == ["rush b site", "gg wp"]
    assert regions[0].bbox == (10, 20, 110, 38)
    assert all(r.source is Source.SCREEN for r in regions)


def test_backend_receives_bgra_bytes_and_size():
    backend = FakeBackend([RawLine("x", 0, 0, 10, 10)])
    _engine(backend).recognize(_frame(w=40, h=30))
    size, width, height, first_alpha = backend.calls[0]
    assert (width, height) == (40, 30)
    assert size == 40 * 30 * 4
    assert first_alpha == 255


def test_empty_lines_are_dropped():
    backend = FakeBackend([RawLine("   ", 0, 0, 10, 10), RawLine("ok", 0, 0, 10, 10)])
    assert [r.text for r in _engine(backend).recognize(_frame())] == ["ok"]


def test_results_are_sorted_top_to_bottom():
    backend = FakeBackend(
        [
            RawLine("second", 0, 50, 40, 16),
            RawLine("first", 0, 10, 40, 16),
        ]
    )
    assert [r.text for r in _engine(backend).recognize(_frame())] == ["first", "second"]


def test_lines_without_geometry_keep_no_bbox():
    """Windows occasionally reports a line with no word rects."""
    backend = FakeBackend([RawLine("no box", 0, 0, 0, 0)])
    regions = _engine(backend).recognize(_frame())
    assert regions[0].bbox is None


def test_confidence_is_always_full_because_windows_reports_none():
    backend = FakeBackend([RawLine("x", 0, 0, 10, 10)])
    assert _engine(backend, min_confidence=0.99).recognize(_frame())[0].confidence == 1.0


# ---------------------------------------------------------------------- scale


def test_upscaled_boxes_are_mapped_back_to_frame_coordinates():
    """With scale=2 the backend sees doubled pixels; bboxes must come back in the
    original frame's coordinate system or every overlay anchor is off by 2x."""
    backend = FakeBackend([RawLine("hello", 20, 40, 100, 20)])
    engine = _engine(backend, scale=2)
    region = engine.recognize(_frame(w=300, h=120))[0]
    assert region.bbox == (10, 20, 60, 30)


def test_scale_is_applied_to_the_image_sent_to_the_backend():
    backend = FakeBackend([RawLine("x", 0, 0, 10, 10)])
    _engine(backend, scale=2).recognize(_frame(w=40, h=30))
    _size, width, height, _a = backend.calls[0]
    assert (width, height) == (80, 60)


# ------------------------------------------------------------------ lifecycle


def test_warmup_is_delegated():
    backend = FakeBackend()
    engine = _engine(backend)
    engine.warmup()
    engine.warmup()
    assert backend.warmed == 2, "engine forwards warmup each time it is asked"


def test_recognize_warms_up_implicitly():
    backend = FakeBackend([RawLine("x", 0, 0, 10, 10)])
    engine = _engine(backend)
    engine.recognize(_frame())
    assert backend.warmed == 1, "recognise must warm up exactly once"
    engine.recognize(_frame())
    assert backend.warmed == 1, "and not again afterwards"


def test_close_drops_the_backend():
    engine = _engine(FakeBackend())
    engine.close()
    assert engine._backend is None


def test_language_is_carried_onto_regions():
    backend = FakeBackend([RawLine("你好", 0, 0, 10, 10)])
    assert _engine(backend, lang="zh").recognize(_frame())[0].lang is Lang.ZH


# ------------------------------------------------------------------ registry


def test_engine_registers_under_windows():
    import translator.ocr.windows_engine  # noqa: F401
    from translator.core.interfaces import OCREngine
    from translator.core.registry import available

    assert "windows" in available(OCREngine)


def test_module_imports_on_linux():
    """Every WinRT import must stay lazy or the module cannot even be listed on
    the Linux build machine."""
    import importlib
    import sys

    module = importlib.import_module("translator.ocr.windows_engine")
    assert module.WindowsOCREngine.name == "windows"
    assert not any(name.startswith("winrt") for name in sys.modules)
