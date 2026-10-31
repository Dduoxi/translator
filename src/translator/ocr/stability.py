"""Require a line to be seen twice before translating it.

The problem this solves: an FPS camera moves constantly, so a fixed ROI over the
chat area also catches world text -- building signs, objective labels, damage
numbers -- that is only inside the box for a frame or two, or that slides across
it.

The one reliable difference between them:

* a **chat line** sits at a fixed screen position and stays there for seconds
* **world text** either moves every frame (camera + parallax) or lives for a
  frame or two (damage numbers)

So: a line is only accepted once it has been seen at the same place with the same
text in ``confirm_frames`` consecutive OCR passes. Moving text never survives;
chat does, at the cost of one extra OCR interval of latency.

Matching is intentionally loose on position (an 8 px grid) and exact on text, so
a flickering glyph or a one-pixel jitter does not reset the count, but different
text never inherits the previous frame's confirmation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..core.dedupe import normalise
from ..core.types import TextRegion

__all__ = ["StabilityGate"]

_GRID = 8
"""Quantise the box centre to this many pixels."""


def _key(region: TextRegion) -> tuple[str, int, int]:
    text = normalise(region.text)
    if region.bbox is None:
        return (text, -1, -1)
    x1, y1, x2, y2 = region.bbox
    return (text, round((x1 + x2) / 2 / _GRID), round((y1 + y2) / 2 / _GRID))


@dataclass
class _Entry:
    hits: int
    last_pass: int


class StabilityGate:
    """Keeps only lines that repeat at the same spot.

    ``confirm_frames <= 1`` disables the gate entirely (returns everything), so it
    costs nothing when unused.
    """

    def __init__(self, confirm_frames: int = 2, max_entries: int = 256) -> None:
        self._needed = max(1, int(confirm_frames))
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, int, int], _Entry] = {}
        self._pass = 0

    @property
    def enabled(self) -> bool:
        return self._needed > 1

    def filter(self, regions: list[TextRegion]) -> tuple[list[TextRegion], int]:
        """Return ``(accepted, rejected_count)`` for one OCR pass."""
        if not self.enabled:
            return regions, 0

        accepted: list[TextRegion] = []
        rejected = 0
        with self._lock:
            self._pass += 1
            seen: dict[tuple[str, int, int], _Entry] = {}
            for region in regions:
                key = _key(region)
                previous = self._entries.get(key)
                # Consecutive means "also seen in the immediately previous pass".
                hits = previous.hits + 1 if previous and previous.last_pass == self._pass - 1 else 1
                seen[key] = _Entry(hits=hits, last_pass=self._pass)
                if hits >= self._needed:
                    accepted.append(region)
                else:
                    rejected += 1

            self._entries = seen
            if len(self._entries) > self._max:
                # Keep the most recent; anything older has been gone for a pass
                # and would be reset anyway.
                ordered = sorted(self._entries.items(), key=lambda kv: -kv[1].last_pass)
                self._entries = dict(ordered[: self._max])
        return accepted, rejected

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._pass = 0
