"""Frame-rate gate: swallow frames that barely changed.

In an FPS the chat box is static the vast majority of the time, so the cheapest
possible OCR optimisation is to not run OCR. This compares a heavily decimated
grayscale signature rather than a full frame diff -- a few microseconds for a
1080p frame.
"""

from __future__ import annotations

import numpy as np

from ..core.interfaces import ScreenSource
from ..core.types import Frame

__all__ = ["ChangedOnly"]


def _signature(image: np.ndarray, cells: int = 64) -> np.ndarray:
    """Decimate to ~cells x cells grayscale, independent of aspect ratio."""
    h, w = image.shape[:2]
    step_y = max(1, h // cells)
    step_x = max(1, w // cells)
    small = image[::step_y, ::step_x, :3].astype(np.float32)
    return small.mean(axis=2)


class ChangedOnly(ScreenSource):
    """Decorator: return ``None`` when the new frame is ~identical to the last.

    ``min_change_ratio`` is the fraction of decimated cells that must differ by
    more than ``pixel_threshold`` levels. 0.004 means "0.4% of the area changed",
    which reliably catches a single new chat line but ignores a twitching HUD.
    """

    name = "changed_only"

    def __init__(
        self,
        inner: ScreenSource,
        min_change_ratio: float = 0.004,
        pixel_threshold: float = 12.0,
        **_ignored: object,
    ) -> None:
        self._inner = inner
        self._min_ratio = min_change_ratio
        self._pixel_threshold = pixel_threshold
        self._last_sig: np.ndarray | None = None
        self.skipped = 0
        self.passed = 0

    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def grab(self) -> Frame | None:
        frame = self._inner.grab()
        if frame is None:
            return None
        sig = _signature(frame.image)  # type: ignore[attr-defined]
        last = self._last_sig
        if last is not None and last.shape == sig.shape:
            ratio = float(np.mean(np.abs(sig - last) > self._pixel_threshold))
            if ratio < self._min_ratio:
                self.skipped += 1
                return None
        self._last_sig = sig
        self.passed += 1
        return frame

    def reset(self) -> None:
        """Force the next frame through (e.g. after the user moves the ROI)."""
        self._last_sig = None

    @property
    def size(self) -> tuple[int, int]:
        return self._inner.size

    def __getattr__(self, item: str) -> object:
        # Transparently expose the wrapped source's extra attributes.
        return getattr(self._inner, item)
