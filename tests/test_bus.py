from fpst.core.bus import EventBus
from fpst.core.types import Lang, Source, Subtitle, TextRegion


def test_subscribe_and_publish():
    bus = EventBus()
    seen = []
    bus.subscribe(Subtitle, seen.append)
    sub = Subtitle("hi", "你好", Source.SCREEN, Lang.EN)
    bus.publish(sub)
    assert seen == [sub]


def test_publish_walks_mro():
    """Subscribing to object catches everything; subscribing to a base type works."""
    bus = EventBus()
    hits = []
    bus.subscribe(object, lambda e: hits.append(type(e).__name__))
    bus.publish(TextRegion("x", Source.VOICE))
    assert hits == ["TextRegion"]


def test_handler_exception_is_isolated():
    bus = EventBus()
    seen = []

    def boom(_e):
        raise RuntimeError("nope")

    bus.subscribe(Subtitle, boom)
    bus.subscribe(Subtitle, seen.append)
    bus.publish(Subtitle("a", "b", Source.VOICE, Lang.EN))
    assert len(seen) == 1


def test_unsubscribe():
    bus = EventBus()
    seen = []
    off = bus.subscribe(Subtitle, seen.append)
    off()
    bus.publish(Subtitle("a", "b", Source.VOICE, Lang.EN))
    assert seen == []


def test_unsubscribe_twice_is_safe():
    bus = EventBus()
    off = bus.subscribe(Subtitle, lambda e: None)
    off()
    off()
