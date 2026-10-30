"""Canned OCR results for tests and for `engine = "mock"` runs."""

from __future__ import annotations

from ..core.interfaces import OCREngine
from ..core.registry import register
from ..core.types import Frame, Lang, Source, TextRegion


class MockOCREngine(OCREngine):
    """Returns scripted lines, one set per call (last set repeats)."""

    name = "mock"

    def __init__(
        self,
        lines: list[str] | None = None,
        lang: str = "en",
        min_confidence: float = 0.0,
        **_ignored: object,
    ) -> None:
        self._script = lines or ["GG WP", "rush b site", "enemy behind the box"]
        self._lang = lang
        self._min_conf = min_confidence
        self._cursor = 0
        self.calls = 0

    def recognize(self, frame: Frame) -> list[TextRegion]:
        self.calls += 1
        if self.calls > 1:
            return []
        out: list[TextRegion] = []
        for i, text in enumerate(self._script):
            out.append(
                TextRegion(
                    text=text,
                    source=Source.SCREEN,
                    lang=Lang(self._lang),
                    confidence=0.99,
                    bbox=(10, 10 + i * 30, 10 + 12 * len(text), 34 + i * 30),
                )
            )
        return out


@register(OCREngine, "mock")
def _factory(**kwargs: object) -> OCREngine:
    return MockOCREngine(**kwargs)  # type: ignore[arg-type]
