from __future__ import annotations

import os

import numpy as np
import pytest

from translator.capture.gate import ChangedOnly
from translator.core.interfaces import ScreenSource
from translator.core.types import Frame


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
    from translator.capture.mss_source import MssSource

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
    from translator.capture.mss_source import MssSource

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

    from translator.capture.mss_source import MssSource

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


# ------------------------------------------------------- engine fallback


def test_unavailable_dxgi_falls_back_to_mss(monkeypatch):
    """On Windows the default is dxgi. It registers fine but needs bettercam,
    which is imported at open() time -- so a missing bettercam used to kill the
    capture thread while the UI still said "running" with zero OCR runs."""
    from translator.capture import make_screen_source
    from translator.core.config import ScreenConfig
    from translator.core.interfaces import ScreenSource
    from translator.core.registry import RegistryError, register

    def broken(**kwargs):
        raise RegistryError("DXGI capture needs bettercam")

    register(ScreenSource, "broken_dxgi")(broken)
    source = make_screen_source(ScreenConfig(engine="broken_dxgi"))
    # ChangedOnly wraps mss; unwrap to see what got built.
    inner = getattr(source, "_inner", source)
    assert type(inner).__name__ == "MssSource", "must not give up when dxgi fails"


def test_mss_failure_is_not_swallowed():
    """If even the fallback is broken there is nothing to fall back to."""
    from translator.capture import make_screen_source
    from translator.core.config import ScreenConfig
    from translator.core.interfaces import ScreenSource
    from translator.core.registry import RegistryError, register

    def boom(**kwargs):
        raise RegistryError("nope")

    register(ScreenSource, "broken_mss")(boom)
    # engine="broken_mss" -> falls back to mss, which works on this machine.
    source = make_screen_source(ScreenConfig(engine="broken_mss"))
    assert source is not None


def test_dxgi_registration_is_probed_at_construction():
    """Constructing DxgiSource without bettercam must fail loudly and early,
    not start a thread that dies on its first grab."""
    import sys

    if sys.platform == "win32":
        pytest.skip("bettercam may be installed on Windows")
    from translator.capture.dxgi import DxgiSource
    from translator.core.registry import RegistryError

    try:
        import bettercam  # noqa: F401

        pytest.skip("bettercam is installed here")
    except ImportError:
        pass

    with pytest.raises(RegistryError, match="bettercam"):
        DxgiSource()
