"""Suppress re-processing of text we have already translated.

The OCR stage re-reads the *whole* ROI every time anything in it changes, so a
chat box with one new message yields the previous four messages too. Without
this, every new line costs five translations and the overlay fills with repeats.

Keyed on a normalised form of the text with a TTL, so a line the user legitimately
sees twice within a few minutes is not shown twice, but a line that scrolls away
and comes back later still is.
"""

from __future__ import annotations

import re
import threading
import time

__all__ = ["RecentTexts", "normalise"]

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse whitespace and case so trivial OCR jitter doesn't defeat dedupe."""
    return _WS.sub(" ", text).strip().casefold()


class RecentTexts:
    def __init__(self, ttl: float = 90.0, max_entries: int = 512) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def check_and_add(self, text: str, now: float | None = None) -> bool:
        """Return True if this text is new (and record it)."""
        key = normalise(text)
        if not key:
            return False
        now = time.monotonic() if now is None else now
        with self._lock:
            self._evict(now)
            if key in self._seen:
                return False
            if len(self._seen) >= self._max:
                # Drop the oldest entry; dicts preserve insertion order.
                self._seen.pop(next(iter(self._seen)), None)
            self._seen[key] = now
            return True

    def __contains__(self, text: str) -> bool:
        with self._lock:
            return normalise(text) in self._seen

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def _evict(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self._ttl]
        for key in expired:
            del self._seen[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)
