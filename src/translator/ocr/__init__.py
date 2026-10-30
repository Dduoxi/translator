"""OCR engines."""

from __future__ import annotations

from collections.abc import Iterable

from ..core.config import OCRConfig
from ..core.interfaces import OCREngine
from ..core.registry import create
from ..core.types import TextRegion
from .lines import drop_duplicates, drop_oversized, merge_horizontal

__all__ = [
    "make_ocr_engine",
    "postprocess",
    "merge_horizontal",
    "drop_duplicates",
    "drop_oversized",
]


def make_ocr_engine(cfg: OCRConfig) -> OCREngine:
    return create(
        OCREngine,
        cfg.engine,
        lang=cfg.lang,
        min_confidence=cfg.min_confidence,
        threads=cfg.threads,
        det_limit_side=cfg.det_limit_side,
        rec_batch=cfg.rec_batch,
        rec_height=cfg.rec_height,
    )


def postprocess(regions: Iterable[TextRegion], merge: bool = True) -> list[TextRegion]:
    regions = drop_oversized(list(regions))
    regions = merge_horizontal(regions) if merge else [r for r in regions if r.text]
    return drop_duplicates(regions)
