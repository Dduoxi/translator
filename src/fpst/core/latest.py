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

__all__ = ["LatestValue", "LatestPerKey"]

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


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


class LatestPerKey(Generic[K, V]):
    """Like :class:`LatestValue`, but one pending slot *per key*.

    With several ROIs on screen, a single-slot mailbox is wrong: two regions can
    change in the same tick and the second would overwrite the first, silently
    losing a translation. Here every region keeps at most one pending frame (an
    old one for that region is still dropped), so nothing is lost and a slow
    consumer cannot build a backlog.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: dict[K, V] = {}
        self._closed = False
        self._drops = 0

    def put(self, key: K, value: V) -> None:
        with self._cv:
            if key in self._pending:
                self._drops += 1  # that region had an unprocessed frame
            self._pending[key] = value
            self._cv.notify()

    def extend(self, key: K, items: list) -> None:
        """Append to the pending list for ``key`` instead of replacing it.

        Used for translation batches: dropping a pending batch would silently
        lose a chat line, so batches for the same region are merged and the
        (larger) request is sent once.
        """
        with self._cv:
            existing = self._pending.get(key)
            if existing is None:
                self._pending[key] = list(items)
            else:
                existing.extend(items)  # type: ignore[attr-defined]
            self._cv.notify()

    def get(self, timeout: float | None = None) -> tuple[K, V] | None:
        with self._cv:
            if not self._cv.wait_for(lambda: bool(self._pending) or self._closed, timeout):
                return None
            if not self._pending:
                return None
            key = next(iter(self._pending))
            return key, self._pending.pop(key)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def stats(self) -> dict[str, int]:
        with self._cv:
            return {"pending": len(self._pending), "dropped": self._drops}
