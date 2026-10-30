from fpst.core.types import Lang, Rect, Source, Subtitle, TextRegion


def test_rect_relative_roundtrip():
    r = Rect.from_relative(0.1, 0.2, 0.5, 0.3, 1920, 1080)
    assert r.xywh == (192, 216, 960, 324)
    rt = r.to_relative(1920, 1080)
    assert all(abs(a - b) < 1e-6 for a, b in zip(rt, (0.1, 0.2, 0.5, 0.3), strict=True))


def test_rect_clamped_to_frame():
    r = Rect.from_relative(0.9, 0.9, 0.5, 0.5, 1000, 1000)
    assert r.x + r.w <= 1000
    assert r.y + r.h <= 1000


def test_rect_rejects_empty():
    import pytest

    with pytest.raises(ValueError):
        Rect(0, 0, 0, 10)


def test_rect_intersect():
    a = Rect(0, 0, 100, 100)
    b = Rect(50, 50, 100, 100)
    assert a.intersect(b) == Rect(50, 50, 50, 50)
    assert a.intersect(Rect(200, 200, 10, 10)) is None


def test_text_region_strips():
    tr = TextRegion(text="  gg wp  ", source=Source.SCREEN)
    assert tr.text == "gg wp"
    assert not tr.is_empty
    assert TextRegion(text="   ", source=Source.VOICE).is_empty


def test_subtitle_gets_unique_id():
    a = Subtitle("hi", "你好", Source.SCREEN, Lang.EN)
    b = Subtitle("hi", "你好", Source.SCREEN, Lang.EN)
    assert a.subtitle_id != b.subtitle_id
