"""Cross-platform screen capture via mss.

Used as the dev-box source (X11/Wayland/DXGI-under-the-hood) and as the Windows
fallback when DXGI Desktop Duplication is unavailable (e.g. some VMs).
"""

from __future__ import annotations

import numpy as np

from ..core.interfaces import ScreenSource
from ..core.registry import register
from ..core.types import Frame, Rect


class MssSource(ScreenSource):
    """Grabs one monitor, optionally cropped to a region of interest.

    mss has no change notification, so ``grab`` always returns a frame; wrap this
    in :class:`fpst.capture.gate.ChangedOnly` if you want cheap skipping.
    """

    name = "mss"

    def __init__(
        self,
        monitor: int = 1,
        region: tuple[float, float, float, float] | None = None,
        **_ignored: object,
    ) -> None:
        self._monitor_index = monitor
        self._region_rel = region
        self._sct = None
        self._bbox: dict[str, int] = {}
        self._offset = (0, 0)
        self._size = (0, 0)

    # ------------------------------------------------------------- lifecycle
    def open(self) -> None:
        if self._sct is not None:
            return
        import mss

        self._sct = mss.MSS()
        monitors = self._sct.monitors
        # monitors[0] is the union of all displays; 1..n are physical monitors.
        index = max(1, min(int(self._monitor_index), len(monitors) - 1))
        mon = monitors[index]
        left, top, width, height = (int(mon[k]) for k in ("left", "top", "width", "height"))

        if self._region_rel is not None:
            roi = Rect.from_relative(*self._region_rel, width, height)
            self._bbox = {
                "left": left + roi.x,
                "top": top + roi.y,
                "width": roi.w,
                "height": roi.h,
            }
        else:
            self._bbox = {"left": left, "top": top, "width": width, "height": height}

        self._offset = (self._bbox["left"], self._bbox["top"])
        self._size = (self._bbox["width"], self._bbox["height"])

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
        self._sct = None

    # ---------------------------------------------------------------- capture
    def grab(self) -> Frame | None:
        if self._sct is None:
            self.open()
        assert self._sct is not None
        shot = self._sct.grab(self._bbox)
        # BGRA -> BGR view then copy, so downstream code gets a compact array.
        raw = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        bgr = raw[:, :, :3].copy()
        return Frame(image=bgr, monitor=self._monitor_index, offset=self._offset)

    @property
    def size(self) -> tuple[int, int]:
        if self._size == (0, 0):
            self.open()
        return self._size


@register(ScreenSource, "mss")
def _factory(**kwargs: object) -> ScreenSource:
    return MssSource(**kwargs)  # type: ignore[arg-type]
