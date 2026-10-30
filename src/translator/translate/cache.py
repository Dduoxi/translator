"""Glossary-aware LRU cache that wraps any :class:`Translator`.

Two jobs:

* **Cache** -- chat is extremely repetitive ("gg", "rush b", player names). A
  hit is a free translation and, on a metered API, a free request.
* **Glossary** -- FPS shorthand is exactly what general MT gets wrong ("rush"
  -> "冲出" instead of the tactical "快攻"). Glossary hits short-circuit the
  engine entirely, which is both more accurate *and* faster than any model.

Deliberately not baked into each engine: this way every engine (including the
mock) benefits, and hit rate is measurable in one place.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence

from ..core.interfaces import Translator
from ..core.types import Lang

__all__ = ["CachingTranslator"]


class CachingTranslator(Translator):
    name = "caching"

    def __init__(
        self,
        inner: Translator,
        maxsize: int = 1024,
        glossary: dict[str, str] | None = None,
    ) -> None:
        self._inner = inner
        self._max = max(1, maxsize)
        self._cache: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._glossary = {k.strip().casefold(): v for k, v in (glossary or {}).items() if k}
        self.hits = 0
        self.misses = 0
        self.glossary_hits = 0

    # ------------------------------------------------------------- lifecycle
    def warmup(self) -> None:
        self._inner.warmup()

    def close(self) -> None:
        self._inner.close()

    # ------------------------------------------------------------- translate
    def translate(self, texts: Sequence[str], src: Lang, dst: Lang) -> list[str]:
        out: list[str | None] = [None] * len(texts)
        pending: list[str] = []
        pending_idx: list[int] = []

        for i, text in enumerate(texts):
            key_text = text.strip()
            if not key_text:
                out[i] = ""
                continue

            glossed = self._glossary.get(key_text.casefold())
            if glossed is not None:
                out[i] = glossed
                self.glossary_hits += 1
                continue

            cache_key = (key_text, src.value, dst.value)
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                out[i] = cached
                self.hits += 1
                continue

            pending.append(key_text)
            pending_idx.append(i)
            self.misses += 1

        if pending:
            translated = self._inner.translate(pending, src, dst)
            if len(translated) != len(pending):
                raise ValueError(
                    f"{type(self._inner).__name__} returned {len(translated)} results "
                    f"for {len(pending)} inputs"
                )
            for idx, text, result in zip(pending_idx, pending, translated, strict=False):
                out[idx] = result
                if result:
                    self._store((text, src.value, dst.value), result)

        return [t or "" for t in out]

    def _store(self, key: tuple[str, str, str], value: str) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    # ----------------------------------------------------------------- stats
    @property
    def stats(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {
            "hits": float(self.hits),
            "misses": float(self.misses),
            "glossary": float(self.glossary_hits),
            "hit_rate": self.hits / total if total else 0.0,
            "size": float(len(self._cache)),
        }

    def clear_cache(self) -> None:
        self._cache.clear()
