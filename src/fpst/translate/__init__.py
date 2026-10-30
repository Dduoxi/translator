"""Translation engines and the caching/glossary decorator."""

from __future__ import annotations

import logging

from ..core.config import TranslateConfig
from ..core.interfaces import Translator
from ..core.registry import create
from ..core.types import Lang

__all__ = ["make_translator", "resolve_engine", "has_api_key"]

log = logging.getLogger(__name__)


def has_baidu(cfg: TranslateConfig) -> bool:
    from ..runtime.credentials import load_baidu

    appid, secret, _ = load_baidu(cfg)
    return bool(appid and secret)


def has_api_key(cfg: TranslateConfig, api_key: str | None = None) -> bool:
    if api_key is not None:
        return bool(api_key.strip())
    from ..runtime.credentials import is_local_endpoint, load_api_key

    if is_local_endpoint(cfg.base_url):
        return True  # loopback models need no key
    return bool(load_api_key(cfg)[0])


def resolve_engine(cfg: TranslateConfig, api_key: str | None = None) -> str:
    """Resolve ``engine = "auto"`` to something concrete."""
    if cfg.engine != "auto":
        return cfg.engine
    # llm first: it is the better translator for game slang, baidu is the
    # cheap/domestic fallback.
    if has_api_key(cfg, api_key):
        return "llm"
    if has_baidu(cfg):
        return "baidu"
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


def make_translator(cfg: TranslateConfig, api_key: str | None = None) -> Translator:
    engine = resolve_engine(cfg, api_key)
    glossary = merged_glossary(cfg)
    from .cache import CachingTranslator

    if engine == "baidu":
        from ..runtime.credentials import load_baidu

        appid, secret, _source = load_baidu(cfg)
        return CachingTranslator(
            create(
                Translator,
                "baidu",
                appid=appid or cfg.baidu_appid or None,
                secret_key=secret or cfg.baidu_key or None,
                appid_env=cfg.baidu_appid_env,
                key_env=cfg.baidu_key_env,
                timeout_s=cfg.timeout_s,
                qps=cfg.baidu_qps,
            ),
            maxsize=cfg.cache_size,
            glossary=glossary,
        )
    if engine == "llm" and api_key is None:
        # Prefer the OS credential store over the config file / env.
        from ..runtime.credentials import load_api_key

        api_key = load_api_key(cfg)[0] or None
    base = create(
        Translator,
        engine,
        glossary=glossary,
        timeout_s=cfg.timeout_s,
        retries=cfg.retries,
        retry_backoff_s=cfg.retry_backoff_s,
        base_url=cfg.base_url,
        model=cfg.model,
        api_key=api_key or cfg.api_key or None,
        api_key_env=cfg.api_key_env,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        context=cfg.context,
        disable_thinking=cfg.disable_thinking,
        extra_params=cfg.extra_params,
    )
    # The cache sits outside the engine so it also covers the mock and any
    # future engine, and so hit-rate is visible in one place.
    return CachingTranslator(base, maxsize=cfg.cache_size, glossary=glossary)


def to_lang(value: str) -> Lang:
    try:
        return Lang(value)
    except ValueError:
        return Lang.AUTO
