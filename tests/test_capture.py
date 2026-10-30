from __future__ import annotations

import os

import numpy as np
import pytest

from fpst.capture.gate import ChangedOnly
from fpst.core.interfaces import ScreenSource
from fpst.core.types import Frame


class FakeSource(ScreenSource):
    """Replays a fixed list of images; then just repeats the last one."""

    name = "fake"

    def __init__(self, images: list[np.ndarray]) -> None:
        self._images = images
        self._i = 0
        self.opened = 0
        self.closed = 0
        self.frames: list[Frame | None] = []

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def grab(self) -> Frame | None:
        img = self._images[min(self._i, len(self._images) - 1)]
        self._i += 1
        frame = None if img is None else Frame(image=img)
        self.frames.append(frame)
        return frame

    @property
    def size(self) -> tuple[int, int]:
        h, w = self._images[0].shape[:2]
        return int(w), int(h)


def black(h: int = 120, w: int = 320) -> np.ndarray:
    return np.zeros((h, w, 3), np.uint8)


def with_patch(h: int = 120, w: int = 320, size: int = 80) -> np.ndarray:
    img = black(h, w)
    img[10 : 10 + size, 10 : 10 + size] = 255
    return img


# ------------------------------------------------------------------ ChangedOnly


def test_first_frame_always_passes():
    gate = ChangedOnly(FakeSource([black(), black()]))
    assert gate.grab() is not None


def test_identical_frames_are_skipped():
    gate = ChangedOnly(FakeSource([black(), black(), black()]), min_change_ratio=0.004)
    assert gate.grab() is not None
    assert gate.grab() is None
    assert gate.grab() is None
    assert (gate.passed, gate.skipped) == (1, 2)


def test_visible_change_passes():
    gate = ChangedOnly(FakeSource([black(), with_patch()]), min_change_ratio=0.004)
    gate.grab()
    assert gate.grab() is not None
    assert gate.passed == 2


def test_tiny_change_below_threshold_is_ignored():
    """A 4x4 patch on 320x120 is ~0.04% of the area -- below 0.4%."""
    gate = ChangedOnly(FakeSource([black(), with_patch(size=4)]), min_change_ratio=0.004)
    gate.grab()
    assert gate.grab() is None


def test_reset_forces_next_frame_through():
    gate = ChangedOnly(FakeSource([black(), black()]))
    gate.grab()
    gate.reset()
    assert gate.grab() is not None


def test_none_from_inner_is_passed_through():
    gate = ChangedOnly(FakeSource([black(), None, black()]))
    gate.grab()
    assert gate.grab() is None  # inner said "nothing changed"
    # The None must not have clobbered the signature: next identical frame is skipped
    assert gate.grab() is None


def test_lifecycle_is_delegated():
    inner = FakeSource([black()])
    gate = ChangedOnly(inner)
    gate.open()
    gate.close()
    assert (inner.opened, inner.closed) == (1, 1)


def test_size_delegates():
    assert ChangedOnly(FakeSource([black(100, 200)])).size == (200, 100)


# -------------------------------------------------------------------- MssSource

has_display = bool(os.environ.get("DISPLAY") or os.name == "nt")


@pytest.mark.slow
@pytest.mark.skipif(not has_display, reason="needs a desktop session")
def test_mss_grabs_real_screen():
    pytest.importorskip("mss")
    from fpst.capture.mss_source import MssSource

    src = MssSource(monitor=1)
    try:
        frame = src.grab()
        assert frame is not None
        w, h = src.size
        assert frame.image.shape == (h, w, 3)
        assert frame.image.dtype == np.uint8
    finally:
        src.close()


@pytest.mark.slow
@pytest.mark.skipif(not has_display, reason="needs a desktop session")
def test_mss_roi_is_respected():
    pytest.importorskip("mss")
    from fpst.capture.mss_source import MssSource

    src = MssSource(monitor=1, region=(0.25, 0.25, 0.5, 0.5))
    try:
        frame = src.grab()
        assert frame is not None
        w, h = src.size
        assert frame.image.shape[:2] == (h, w)
        full_w, full_h = MssSource(monitor=1).size
        assert w == pytest.approx(full_w // 2, abs=2)
        assert h == pytest.approx(full_h // 2, abs=2)
    finally:
        src.close()


@pytest.mark.slow
@pytest.mark.skipif(not has_display, reason="needs a desktop session")
def test_monitor_is_matched_by_origin_not_index():
    """mss inserts an all-displays entry at index 0 while Qt does not, so the two
    libraries disagree about which screen is #1. Position is unambiguous."""
    pytest.importorskip("mss")
    import mss as _mss

    from fpst.capture.mss_source import MssSource

    with _mss.MSS() as sct:
        monitors = sct.monitors
    if len(monitors) < 3:
        pytest.skip("needs at least two physical monitors")

    # Ask for the *second* physical monitor but give the origin of the first.
    first = monitors[1]
    src = MssSource(
        monitor=2,  # deliberately wrong index
        monitor_origin=(int(first["left"]), int(first["top"])),
    )
    try:
        width, height = src.size
    finally:
        src.close()
    assert (width, height) == (int(first["width"]), int(first["height"]))
