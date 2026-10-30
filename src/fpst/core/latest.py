"""A capacity-1 mailbox: producers overwrite, the consumer always gets the newest.

This is the load-bearing primitive of the whole capture pipeline. OCR takes
~400 ms; the screen refreshes at 60 Hz. A normal queue would accumulate 24
stale frames and process them one by one -- the subtitles would trail reality by
ten seconds and memory would grow without bound.

What we actually want is "translate the current screen state", not "translate
every frame". So the mailbox keeps exactly one item: if the consumer is slow,
frames are dropped silently, which is both the correct semantic and the
back-pressure mechanism.
"""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

__all__ = ["LatestValue"]

T = TypeVar("T")


class LatestValue(Generic[T]):
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._item: T | None = None
        self._has_item = False
        self._seq = 0
        self._put_count = 0
        self._overwrites = 0
        self._closed = False

    def put(self, item: T) -> None:
        with self._cv:
            if self._has_item:
                self._overwrites += 1  # a frame nobody ever looked at
            self._item = item
            self._has_item = True
            self._seq += 1
            self._put_count += 1
            self._cv.notify()

    def get(self, timeout: float | None = None) -> tuple[int, T] | None:
        """Return ``(seq, item)`` or ``None`` on timeout / close."""
        with self._cv:
            if not self._cv.wait_for(lambda: self._has_item or self._closed, timeout):
                return None
            if not self._has_item:
                return None  # closed
            item, seq = self._item, self._seq
            self._item = None
            self._has_item = False
            return seq, item  # type: ignore[return-value]

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def stats(self) -> dict[str, int]:
        with self._cv:
            return {"put": self._put_count, "dropped": self._overwrites}
