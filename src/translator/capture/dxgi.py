"""Windows Desktop Duplication capture (DXGI), via bettercam/dxcam.

Why not GDI: Desktop Duplication is GPU-side and returns ``None`` on timeout,
which is exactly the "frame unchanged" signal the pipeline wants -- zero cost.

**Thread affinity warning**: the underlying COM objects are bound to the thread
that creates them, so ``open()``/``grab()``/``close()`` must all be called from
the *same* worker thread. Never create the camera on the GUI thread.
"""

from __future__ import annotations

import contextlib
import logging
import sys

import numpy as np

from ..core.interfaces import ScreenSource
from ..core.registry import register
from ..core.types import Frame, Rect

log = logging.getLogger(__name__)

__all__ = ["DxgiSource"]


def _import_backend():
    """bettercam is the maintained fork of dxcam; accept either."""
    try:
        import bettercam

        return bettercam, "bettercam"
    except ImportError:
        pass
    try:
        import dxcam

        return dxcam, "dxcam"
    except ImportError as exc:  # pragma: no cover - Windows only
        raise ImportError(
            "DXGI capture needs `bettercam` (or `dxcam`): uv pip install bettercam"
        ) from exc


class DxgiSource(ScreenSource):
    name = "dxgi"

    def __init__(
        self,
        monitor: int = 1,
        region: tuple[float, float, float, float] | None = None,
        device_idx: int = 0,
        **_ignored: object,
    ) -> None:
        self._monitor_index = int(monitor)
        self._region_rel = region
        self._device_idx = device_idx
        self._backend = None
        self._backend_name = ""
        self._camera = None
        self._offset = (0, 0)
        self._size = (0, 0)

    # ------------------------------------------------------------- lifecycle
    def open(self) -> None:
        if self._camera is not None:
            return
        self._backend, self._backend_name = _import_backend()
        output_idx = max(0, self._monitor_index - 1)
        camera = self._create(output_idx)
        width, height = int(camera.width), int(camera.height)

        if self._region_rel is not None:
            roi = Rect.from_relative(*self._region_rel, width, height)
            # Re-create pinned to the ROI: capturing less is strictly cheaper.
            self._close_camera(camera)
            camera = self._create(output_idx, region=roi.x2y2)
            self._offset = (roi.x, roi.y)
            self._size = (roi.w, roi.h)
        else:
            self._offset = (0, 0)
            self._size = (width, height)

        self._camera = camera
        log.info(
            "DXGI capture via %s: output=%d size=%dx%d offset=%s",
            self._backend_name,
            output_idx,
            self._size[0],
            self._size[1],
            self._offset,
        )

    def _create(self, output_idx: int, region: tuple[int, int, int, int] | None = None):
        assert self._backend is not None
        # BGR keeps the whole pipeline in OpenCV/RapidOCR's native order.
        kwargs = {
            "device_idx": self._device_idx,
            "output_idx": output_idx,
            "output_color": "BGR",
        }
        if region is not None:
            kwargs["region"] = region
        try:
            return self._backend.create(**kwargs)
        except Exception:  # noqa: BLE001 - output_idx may not exist
            log.warning("output_idx=%d unavailable, falling back to default", output_idx)
            kwargs.pop("output_idx", None)
            return self._backend.create(**kwargs)

    def _close_camera(self, camera) -> None:
        with contextlib.suppress(Exception):
            camera.release()

    def close(self) -> None:
        if self._camera is not None:
            self._close_camera(self._camera)
        self._camera = None

    # ---------------------------------------------------------------- capture
    def grab(self) -> Frame | None:
        if self._camera is None:
            self.open()
        assert self._camera is not None
        image = self._camera.grab()
        if image is None:
            return None  # Desktop Duplication timed out -> nothing changed
        if not isinstance(image, np.ndarray):
            return None
        return Frame(image=image, monitor=self._monitor_index, offset=self._offset)

    @property
    def size(self) -> tuple[int, int]:
        if self._size == (0, 0):
            self.open()
        return self._size


# Register only where the backend can exist, so `doctor` does not advertise
# an engine that would fail on import on Linux.
if sys.platform == "win32":

    @register(ScreenSource, "dxgi")
    def _factory(**kwargs: object) -> ScreenSource:
        return DxgiSource(**kwargs)  # type: ignore[arg-type]
