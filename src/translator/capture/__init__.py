"""Screen capture sources.

``make_screen_sources`` builds one source per configured ROI, each wrapped in its
own change gate.

Why one source per ROI rather than one big capture: the ROIs may be in opposite
corners (chat bottom-left, kill feed top-right), so their union can be the whole
screen. Grabbing each small rect separately is proportional to the *total scanned
area* instead of the bounding box, and it keeps the per-region change gate
simple -- a twitch in the kill feed no longer re-OCRs the chat box.
"""

from __future__ import annotations

import logging

from ..core.config import ScreenConfig
from ..core.interfaces import ScreenSource
from ..core.registry import RegistryError, available, create
from .gate import ChangedOnly

__all__ = ["make_screen_sources", "make_screen_source", "ChangedOnly", "describe"]

log = logging.getLogger(__name__)

PLATFORM_DEFAULT = "mss"
"""mss on every platform, including Windows.

dxgi (Desktop Duplication) is cheaper per grab, but it only produces a frame when
the desktop image actually changes. On a static screen it returns None forever --
including the very first grab -- so the pipeline never gets a frame to OCR and the
app looks broken. For small ROIs mss costs ~2-3 ms, which is a price worth paying
for "it always returns a picture". Set [screen] engine = "dxgi" to opt in.
"""


def _build_one(cfg: ScreenConfig, region: tuple[float, float, float, float] | None) -> ScreenSource:
    name = cfg.engine
    if name == "auto":
        name = PLATFORM_DEFAULT
    try:
        source = create(
            ScreenSource,
            name,
            monitor=cfg.monitor,
            region=region,
            monitor_origin=cfg.monitor_origin,
        )
    except RegistryError as exc:
        if name == "mss":
            raise
        # Not just "not registered here": an engine can be registered yet be
        # unusable at runtime (dxgi without bettercam). Falling back beats
        # capturing nothing at all, which is indistinguishable from "broken".
        log.warning("capture engine %r unusable (%s); falling back to mss", name, exc)
        source = create(
            ScreenSource,
            "mss",
            monitor=cfg.monitor,
            region=region,
            monitor_origin=cfg.monitor_origin,
        )

    if cfg.min_change_ratio > 0:
        source = ChangedOnly(source, min_change_ratio=cfg.min_change_ratio)
    return source


def make_screen_sources(cfg: ScreenConfig) -> list[ScreenSource]:
    """One gated source per ROI, in configured order."""
    return [_build_one(cfg, region) for region in cfg.resolved_regions()]


def make_screen_source(cfg: ScreenConfig) -> ScreenSource:
    """Single-source convenience wrapper (first ROI only)."""
    return make_screen_sources(cfg)[0]


def describe() -> str:
    return f"default={PLATFORM_DEFAULT} available={available(ScreenSource)}"
