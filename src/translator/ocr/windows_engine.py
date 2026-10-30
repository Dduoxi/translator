"""OCR via the Windows built-in recogniser (``Windows.Media.Ocr``).

Why bother when RapidOCR works: the bundled PP-OCRv4 models cost 16 MB on disk
and 60-250 ms of CPU per frame, which is the single largest cost in the pipeline.
The OS recogniser ships with Windows, needs no model download, and runs in a few
milliseconds.

Two constraints shape this file:

* **It only exists on Windows 10+.** Every WinRT import is therefore lazy, so the
  module can be imported (and the engine listed) on Linux.
* **It cannot be unit-tested on the build machine.** So the WinRT call is behind
  a tiny backend interface (:class:`WinRTBackend`); everything above it -- image
  conversion, result mapping, ordering, filtering -- is pure Python and covered by
  tests using a fake backend. If the WinRT part is wrong, it is wrong in one
  small method.

Accuracy note: Windows OCR reports no per-line confidence, so ``confidence`` is
always 1.0 and ``min_confidence`` cannot filter anything here. It also expects
BGRA8, while our frames are BGR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..core.interfaces import OCREngine
from ..core.registry import register
from ..core.types import Frame, Lang, Source, TextRegion

log = logging.getLogger(__name__)

__all__ = ["WindowsOCREngine", "RawLine", "WinRTBackend"]

# Windows language tags differ from ours for Chinese.
_LANG_TAGS = {
    Lang.EN: "en-US",
    Lang.ZH: "zh-Hans-CN",
    Lang.JA: "ja-JP",
    Lang.KO: "ko-KR",
    Lang.RU: "ru-RU",
}


@dataclass(frozen=True)
class RawLine:
    """One recognised line, in frame pixel coordinates."""

    text: str
    x: int
    y: int
    w: int
    h: int


class OcrBackend(Protocol):
    def recognize(self, bgra: bytes, width: int, height: int) -> list[RawLine]: ...


class WinRTBackend:
    """Thin adapter over ``Windows.Media.Ocr``.

    Kept deliberately dumb: convert, call, map. Anything that could be wrong in a
    way that is hard to debug on a Linux build machine lives in here, and nowhere
    else.
    """

    def __init__(self, lang: Lang = Lang.EN) -> None:
        self._lang_tag = _LANG_TAGS.get(lang, "en-US")
        self._engine = None

    def warmup(self) -> None:
        if self._engine is not None:
            return
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
        except ImportError as exc:  # pragma: no cover - Windows only
            raise ImportError(
                "Windows OCR needs the WinRT projections: "
                "pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
                "winrt-Windows.Storage.Streams winrt-Windows.Globalization "
                "winrt-Windows.Foundation winrt-runtime"
            ) from exc

        engine = OcrEngine.try_create_from_language(Language(self._lang_tag))
        if engine is None:
            available = ", ".join(
                lang.language_tag for lang in OcrEngine.available_recognizer_languages
            )
            raise RuntimeError(
                f"Windows OCR has no recogniser for {self._lang_tag!r}. Installed: "
                f"{available or '(none)'}. Add the language under "
                f"Settings > Time & Language > Language > Optional features."
            )
        self._engine = engine

    def recognize(self, bgra: bytes, width: int, height: int) -> list[RawLine]:
        if self._engine is None:
            self.warmup()
        try:
            from winrt.windows.graphics.imaging import (
                BitmapAlphaMode,
                BitmapPixelFormat,
                SoftwareBitmap,
            )
            from winrt.windows.storage.streams import DataWriter
        except ImportError as exc:  # pragma: no cover - Windows only
            raise ImportError("WinRT imaging types unavailable") from exc

        writer = DataWriter()
        writer.write_bytes(bgra)
        buffer = writer.detach_buffer()
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            buffer, BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.PREMULTIPLIED
        )

        result = _await(self._engine.recognize_async(bitmap))
        lines: list[RawLine] = []
        for line in result.lines:
            text = (line.text or "").strip()
            if not text:
                continue
            rects = [w.bounding_rect for w in line.words]
            if rects:
                left = int(min(r.x for r in rects))
                top = int(min(r.y for r in rects))
                right = int(max(r.x + r.width for r in rects))
                bottom = int(max(r.y + r.height for r in rects))
            else:
                left = top = right = bottom = 0
            lines.append(RawLine(text, left, top, max(0, right - left), max(0, bottom - top)))
        return lines


def _await(operation, timeout: float = 5.0):
    """Resolve a WinRT IAsyncOperation.

    WinRT operations are awaitable but need an event loop; the OCR stage runs on a
    plain worker thread with none, so one is created per call. At 1-4 Hz that is
    irrelevant next to the recognition itself.
    """
    import asyncio

    async def runner():
        return await asyncio.wait_for(_wrap(operation), timeout)

    return asyncio.run(runner())


async def _wrap(operation):  # pragma: no cover - needs WinRT
    return await operation


class WindowsOCREngine(OCREngine):
    """``OCREngine`` on top of the OS recogniser."""

    name = "windows"
    available_on_platform = ("win32",)

    def __init__(
        self,
        lang: str = "en",
        min_confidence: float = 0.0,
        scale: float = 1.0,
        backend: OcrBackend | None = None,
        **_ignored: object,
    ) -> None:
        self._lang = _coerce_lang(lang)
        self._min_conf = min_confidence
        self._scale = max(1.0, float(scale))
        self._backend = backend
        self._backend_cls = WinRTBackend
        self._warmed = False

    # ------------------------------------------------------------- lifecycle
    def warmup(self) -> None:
        if self._backend is None:
            self._backend = self._backend_cls(lang=self._lang)
        warmup = getattr(self._backend, "warmup", None)
        if warmup is not None:
            warmup()
        self._warmed = True

    def close(self) -> None:
        self._backend = None
        self._warmed = False

    # ------------------------------------------------------------- recognise
    def recognize(self, frame: Frame) -> list[TextRegion]:
        if not self._warmed:
            self.warmup()
        assert self._backend is not None

        image = frame.image
        scale = self._scale
        if scale != 1.0:
            image = _upscale(image, scale)

        bgra = _to_bgra(image)
        height, width = image.shape[:2]
        raw = self._backend.recognize(bgra, int(width), int(height))

        regions: list[TextRegion] = []
        for line in raw:
            text = line.text.strip()
            if not text:
                continue
            bbox = None
            if line.w > 0 and line.h > 0:
                bbox = (
                    int(line.x / scale),
                    int(line.y / scale),
                    int((line.x + line.w) / scale),
                    int((line.y + line.h) / scale),
                )
            regions.append(
                TextRegion(
                    text=text,
                    source=Source.SCREEN,
                    lang=self._lang,
                    confidence=1.0,
                    bbox=bbox,
                )
            )
        regions.sort(key=lambda r: (r.bbox[1] // 8, r.bbox[0]) if r.bbox else (0, 0))
        return regions


def _coerce_lang(value: str) -> Lang:
    try:
        return Lang(value)
    except ValueError:
        return Lang.AUTO


def _to_bgra(image: np.ndarray) -> bytes:
    """BGR (our frame format) -> BGRA bytes, which is what SoftwareBitmap wants."""
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError(f"expected a BGR image, got shape {image.shape}")
    rgb = image[:, :, :3]
    alpha = np.full((*rgb.shape[:2], 1), 255, dtype=np.uint8)
    return np.ascontiguousarray(np.concatenate([rgb, alpha], axis=2)).tobytes()


def _upscale(image: np.ndarray, scale: float) -> np.ndarray:
    """Nearest-neighbour upscale.

    The OS recogniser wants glyphs of roughly 20+ px; game UI text is often
    smaller. Nearest-neighbour is enough -- it adds no interpolation blur for the
    recogniser to fight.
    """
    step = max(2, round(scale))
    return np.repeat(np.repeat(image, step, axis=0), step, axis=1)


@register(OCREngine, "windows")
def _factory(**kwargs: object) -> OCREngine:
    return WindowsOCREngine(**kwargs)  # type: ignore[arg-type]
