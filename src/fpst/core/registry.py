"""Name -> factory registry, so engines are config-selectable.

Usage::

    @register(Translator, "mock")
    def _make_mock(**kw) -> Translator: ...

    tr = create(Translator, "mock")
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["register", "create", "available", "RegistryError"]

T = TypeVar("T")

_FACTORIES: dict[type, dict[str, Callable[..., Any]]] = {}


class RegistryError(LookupError):
    pass


def register(kind: type, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(factory: Callable[..., Any]) -> Callable[..., Any]:
        bucket = _FACTORIES.setdefault(kind, {})
        if name in bucket:
            raise RegistryError(f"{kind.__name__}/{name!r} already registered")
        bucket[name] = factory
        return factory

    return deco


def create(kind: type[T], name: str, **kwargs: Any) -> T:
    bucket = _FACTORIES.get(kind)
    if not bucket:
        raise RegistryError(f"no factories registered for {kind.__name__}")
    try:
        factory = bucket[name]
    except KeyError:
        known = ", ".join(sorted(bucket)) or "<none>"
        raise RegistryError(f"unknown {kind.__name__} {name!r}; available: {known}") from None
    return factory(**kwargs)  # type: ignore[no-any-return]


def available(kind: type) -> list[str]:
    return sorted(_FACTORIES.get(kind, {}))
