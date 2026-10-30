"""A tiny thread-safe, type-keyed event bus.

Two pipelines (screen OCR and voice) run in separate threads and must never
block each other, so handlers are dispatched synchronously but exceptions are
swallowed and reported -- one broken subscriber can't stall the bus.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["EventBus"]

log = logging.getLogger(__name__)

E = TypeVar("E")
Handler = Callable[[Any], None]


class EventBus:
    """Subscribe by event type; publishing walks the MRO so base classes match."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: dict[type, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> Callable[[], None]:
        with self._lock:
            self._handlers[event_type].append(handler)  # type: ignore[arg-type]

        def unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._handlers[event_type].remove(handler)  # type: ignore[arg-type]

        return unsubscribe

    def publish(self, event: Any) -> None:
        targets: list[Handler] = []
        with self._lock:
            for klass in type(event).__mro__:
                targets.extend(self._handlers.get(klass, ()))
        for handler in targets:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - isolation is the whole point
                log.exception("subscriber %r failed on %r", handler, type(event).__name__)

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
