"""Translation engines and the caching/glossary decorator."""

from __future__ import annotations

from ..core.config import TranslateConfig
from ..core.interfaces import Translator
from ..core.registry import create
from ..core.types import Lang

__all__ = ["make_translator"]


def make_translator(cfg: TranslateConfig) -> Translator:
    base = create(
        Translator,
        cfg.engine,
        glossary=cfg.glossary,
        timeout_s=cfg.timeout_s,
    )
    # The cache sits outside the engine so it also covers the mock and any
    # future engine, and so hit-rate is visible in one place.
    from .cache import CachingTranslator

    return CachingTranslator(base, maxsize=cfg.cache_size, glossary=cfg.glossary)


def to_lang(value: str) -> Lang:
    try:
        return Lang(value)
    except ValueError:
        return Lang.AUTO
