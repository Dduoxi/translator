"""The five seams that keep every heavy dependency swappable.

Everything the app does is one of:
    ScreenSource -> OCREngine  -\\
                                  >- Translator -> Subtitle
    AudioSource -> VoiceSegmenter -> ASREngine -/

Concrete engines register themselves by name (see ``fpst.core.registry``) so a
config file can pick ``rapidocr`` today and ``paddle`` tomorrow without touching
the pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import ClassVar

from .types import AudioChunk, Frame, Lang, TextRegion

__all__ = [
    "ScreenSource",
    "OCREngine",
    "AudioSource",
    "VoiceSegmenter",
    "ASREngine",
    "Translator",
]


class _Base:
    """Shared lifecycle helpers. ``name`` is the registry key.

    Deliberately not an ABC: the concrete engines below are, and this only
    contributes optional hooks that most implementations ignore.
    """

    name: ClassVar[str] = ""

    def warmup(self) -> None:
        """Preload models / open connections. Optional; called off the hot path."""

    def close(self) -> None:
        """Release native resources. Must be idempotent."""

    def __enter__(self):
        self.warmup()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ScreenSource(_Base, ABC):
    """Grabs the desktop (or one monitor) as BGR frames."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def grab(self) -> Frame | None:
        """Return the next frame, or ``None`` when nothing changed.

        Returning ``None`` instead of a stale frame is what lets the OCR stage
        cheaply skip work: in an FPS the chat area is static most of the time.
        """

    @property
    @abstractmethod
    def size(self) -> tuple[int, int]:
        """(width, height) of the captured surface in pixels."""


class OCREngine(_Base, ABC):
    """Turns an image into text lines."""

    @abstractmethod
    def recognize(self, frame: Frame) -> list[TextRegion]:
        """Detect and read text. Must return regions sorted top-to-bottom."""


class AudioSource(_Base, ABC):
    """Pushes mono float32 PCM to a callback from a capture thread."""

    @abstractmethod
    def open(self, on_samples: Callable[[AudioChunk], None]) -> None:
        """Start capturing. ``on_samples`` is called on the capture thread and
        must return quickly (just enqueue)."""

    @property
    @abstractmethod
    def sample_rate(self) -> int: ...


class VoiceSegmenter(_Base, ABC):
    """Cuts a continuous stream into speech-sized utterances (VAD)."""

    @abstractmethod
    def push(self, chunk: AudioChunk) -> list[AudioChunk]:
        """Feed audio; return any utterances that just completed."""

    @abstractmethod
    def flush(self) -> list[AudioChunk]:
        """Force-close the in-progress utterance (e.g. on stop)."""


class ASREngine(_Base, ABC):
    """Speech -> text."""

    @abstractmethod
    def transcribe(self, chunk: AudioChunk, lang: Lang = Lang.AUTO) -> TextRegion:
        """Transcribe one utterance. Returns an empty ``TextRegion`` on silence."""


class Translator(_Base, ABC):
    """Batch text translation."""

    @abstractmethod
    def translate(self, texts: Sequence[str], src: Lang, dst: Lang) -> list[str]:
        """Translate in order; must return exactly ``len(texts)`` items.

        Batch-shaped because remote APIs cost one round-trip per call: two lines
        of chat should be one request, not two.
        """

    def translate_one(self, text: str, src: Lang, dst: Lang) -> str:
        return self.translate([text], src, dst)[0]
