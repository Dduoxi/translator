"""Offline translator used by tests and by ``engine = "mock"`` runs.

It maps glossary hits, otherwise emits a deterministic transliteration-ish
marker so you can see something happen end to end without an API key.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..core.interfaces import Translator
from ..core.registry import register
from ..core.types import Lang


class MockTranslator(Translator):
    name = "mock"

    def __init__(self, glossary: dict[str, str] | None = None, **_ignored: object) -> None:
        self._glossary = {k.lower(): v for k, v in (glossary or {}).items()}
        self.calls: list[list[str]] = []

    def translate(self, texts: Sequence[str], src: Lang, dst: Lang) -> list[str]:
        self.calls.append(list(texts))
        return [self._one(t, dst) for t in texts]

    def _one(self, text: str, dst: Lang) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        known = self._glossary.get(stripped.lower())
        if known is not None:
            return known
        return f"⟦{dst.value}⟧{stripped}"


@register(Translator, "mock")
def _factory(**kwargs: object) -> Translator:
    return MockTranslator(**kwargs)  # type: ignore[arg-type]
