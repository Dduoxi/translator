"""Screen capture sources.

``make_screen_source`` picks a platform default and wraps it in the change gate.
"""

from __future__ import annotations

import sys

from ..core.config import ScreenConfig
from ..core.interfaces import ScreenSource
from ..core.registry import RegistryError, available, create
from .gate import ChangedOnly

__all__ = ["make_screen_source", "ChangedOnly"]

PLATFORM_DEFAULT = "dxgi" if sys.platform == "win32" else "mss"


def make_screen_source(cfg: ScreenConfig) -> ScreenSource:
    name = cfg.engine
    if name == "auto":
        name = PLATFORM_DEFAULT

    try:
        source = create(ScreenSource, name, monitor=cfg.monitor, region=cfg.region)
    except RegistryError:
        if name == PLATFORM_DEFAULT:
            raise
        # Configured engine is not installed / not supported here -> fall back.
        source = create(ScreenSource, "mss", monitor=cfg.monitor, region=cfg.region)

    if cfg.min_change_ratio > 0:
        source = ChangedOnly(source, min_change_ratio=cfg.min_change_ratio)
    return source


def describe() -> str:
    return f"default={PLATFORM_DEFAULT} available={available(ScreenSource)}"
