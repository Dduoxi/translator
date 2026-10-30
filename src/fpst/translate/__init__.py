"""Translation engines and the caching/glossary decorator."""

from __future__ import annotations

import logging
import os

from ..core.config import TranslateConfig
from ..core.interfaces import Translator
from ..core.registry import create
from ..core.types import Lang

__all__ = ["make_translator", "resolve_engine", "has_api_key"]

log = logging.getLogger(__name__)


def has_api_key(cfg: TranslateConfig) -> bool:
    return bool(cfg.api_key.strip() or os.environ.get(cfg.api_key_env, "").strip())


def resolve_engine(cfg: TranslateConfig) -> str:
    """Resolve ``engine = "auto"`` to something concrete."""
    if cfg.engine != "auto":
        return cfg.engine
    if has_api_key(cfg):
        return "llm"
    log.warning(
        "no API key found in %s or [translate] api_key -- falling back to the mock "
        "translator (output will be prefixed with a language tag, not real Chinese). "
        "Set the key, or point [translate] base_url at a local Ollama.",
        cfg.api_key_env,
    )
    return "mock"


def merged_glossary(cfg: TranslateConfig) -> dict[str, str]:
    """``glossary`` replaces the defaults; ``glossary_extra`` layers on top."""
    merged = dict(cfg.glossary)
    merged.update(cfg.glossary_extra)
    return {k: v for k, v in merged.items() if k.strip() and v.strip()}


def make_translator(cfg: TranslateConfig) -> Translator:
    engine = resolve_engine(cfg)
    glossary = merged_glossary(cfg)
    base = create(
        Translator,
        engine,
        glossary=glossary,
        timeout_s=cfg.timeout_s,
        base_url=cfg.base_url,
        model=cfg.model,
        api_key=cfg.api_key or None,
        api_key_env=cfg.api_key_env,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        context=cfg.context,
    )
    # The cache sits outside the engine so it also covers the mock and any
    # future engine, and so hit-rate is visible in one place.
    from .cache import CachingTranslator

    return CachingTranslator(base, maxsize=cfg.cache_size, glossary=glossary)


def to_lang(value: str) -> Lang:
    try:
        return Lang(value)
    except ValueError:
        return Lang.AUTO
