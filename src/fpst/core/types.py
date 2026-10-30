"""Core value types shared by every layer.

Nothing in this module may import a platform-specific package: it has to stay
importable on the Linux dev box *and* on the Windows target machine.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Lang",
    "Source",
    "Rect",
    "TextRegion",
    "Subtitle",
    "AudioChunk",
    "Frame",
]


class Lang(StrEnum):
    """Language tag. Deliberately a small closed set."""

    AUTO = "auto"
    EN = "en"
    ZH = "zh"
    JA = "ja"
    KO = "ko"
    RU = "ru"


class Source(StrEnum):
    """Where a piece of text came from."""

    SCREEN = "screen"
    VOICE = "voice"


@dataclass(frozen=True, slots=True)
class Rect:
    """Pixel rectangle in *captured frame* coordinates (origin = top-left)."""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"Rect must have positive size, got {self.w}x{self.h}")

    @property
    def xywh(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    @property
    def x2y2(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def area(self) -> int:
        return self.w * self.h

    def intersect(self, other: Rect) -> Rect | None:
        x1 = max(self.x, other.x)
        y1 = max(self.y, other.y)
        x2 = min(self.x + self.w, other.x + other.w)
        y2 = min(self.y + self.h, other.y + other.h)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    @classmethod
    def from_relative(
        cls, rx: float, ry: float, rw: float, rh: float, frame_w: int, frame_h: int
    ) -> Rect:
        """Build a Rect from fractional coordinates (0..1) of the frame.

        Users pick the chat / subtitle area with the mouse; storing it as
        fractions keeps the profile valid across resolutions.
        """
        x = round(rx * frame_w)
        y = round(ry * frame_h)
        w = max(1, round(rw * frame_w))
        h = max(1, round(rh * frame_h))
        x = max(0, min(x, frame_w - 1))
        y = max(0, min(y, frame_h - 1))
        w = min(w, frame_w - x)
        h = min(h, frame_h - y)
        return cls(x, y, w, h)

    def to_relative(self, frame_w: int, frame_h: int) -> tuple[float, float, float, float]:
        return (self.x / frame_w, self.y / frame_h, self.w / frame_w, self.h / frame_h)


@dataclass(frozen=True, slots=True)
class TextRegion:
    """One raw recognition / transcription result, before translation."""

    text: str
    source: Source
    lang: Lang = Lang.AUTO
    confidence: float = 1.0
    bbox: tuple[int, int, int, int] | None = None  # x1, y1, x2, y2
    ts: float = field(default_factory=time.time)
    meta: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", self.text.strip())

    @property
    def is_empty(self) -> bool:
        return not self.text


@dataclass(frozen=True, slots=True)
class Subtitle:
    """A finished, translated line ready to be drawn in the overlay."""

    src_text: str
    dst_text: str
    source: Source
    src_lang: Lang
    ts: float = field(default_factory=time.time)
    subtitle_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    speaker: str | None = None
    region: int = 0
    """Which ROI this came from, so the overlay can anchor it to that region."""
    latency_ms: float = 0.0
    """End-to-end time from capture/audio-callback to translated text."""
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class Frame:
    """A captured screen region, as BGR uint8 (OpenCV convention)."""

    image: object  # numpy.ndarray, typed loosely to keep numpy out of the type graph
    ts: float = field(default_factory=time.time)
    monitor: int = 0
    offset: tuple[int, int] = (0, 0)
    """Top-left of this frame within the virtual desktop."""
    scale: float = 1.0
    """Capture scaling applied by the OS / by us; bboxes are in *this* frame's px."""

    @property
    def shape(self) -> tuple[int, int]:
        h, w = self.image.shape[:2]  # type: ignore[attr-defined]
        return (int(w), int(h))


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """A block of mono float32 PCM in [-1, 1]."""

    samples: object  # numpy.ndarray float32, 1-D
    sample_rate: int
    ts: float = field(default_factory=time.time)

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate  # type: ignore[arg-type]
