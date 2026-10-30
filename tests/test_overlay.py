from __future__ import annotations

import os
import threading
import time

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fpst.core.bus import EventBus  # noqa: E402
from fpst.core.config import UIConfig  # noqa: E402
from fpst.core.types import Lang, Source, Subtitle  # noqa: E402
from fpst.ui.overlay import OverlayController, SubtitleOverlay  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _cfg(**kwargs) -> UIConfig:
    cfg = UIConfig()
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def overlay(app):
    widget = SubtitleOverlay(_cfg(max_lines=4, line_ttl=5.0), monitor=1)
    yield widget
    widget.clear()
    widget.close()


def _sub(text: str = "打得好", source: Source = Source.SCREEN) -> Subtitle:
    return Subtitle(
        src_text="gg wp", dst_text=text, source=source, src_lang=Lang.EN, latency_ms=800.0
    )


# --------------------------------------------------------------------- widget


def test_add_subtitle_starts_the_refresh_timer(overlay):
    assert not overlay._timer.isActive()
    overlay.add_subtitle(_sub())
    assert overlay._timer.isActive(), "fade animation needs a timer"
    assert len(overlay._lines) == 1


def test_empty_translation_is_ignored(overlay):
    """A skipped noise line must not produce an empty bubble."""
    overlay.add_subtitle(_sub(text="   "))
    assert overlay._lines == []


def test_max_lines_limits_what_is_rendered(overlay):
    for i in range(10):
        overlay.add_subtitle(_sub(text=f"line {i}"))
    visible = overlay._alive(time.monotonic())
    assert len(visible) == 4
    assert visible[-1][0].text == "line 9", "newest line must be the one closest to the anchor"


def test_lines_expire_after_ttl(overlay):
    overlay.add_subtitle(_sub())
    assert overlay._alive(time.monotonic())
    assert overlay._alive(time.monotonic() + 6.0) == []


def test_alpha_fades_in_then_out(overlay):
    """No hard cut: the line ramps up, holds, then eases out."""
    assert overlay._alpha(0.0) == 0.0, "must not pop in at full opacity"
    assert 0.0 < overlay._alpha(0.1) < 1.0, "should be mid-ramp"
    assert overlay._alpha(1.0) == 1.0, "fully visible after the ramp"
    assert overlay._alpha(2.0) == 1.0
    mid_fade = overlay._alpha(4.5)  # ttl 5.0, fade 1.5
    assert 0.0 < mid_fade < 1.0
    assert overlay._alpha(5.0) == 0.0


def test_fade_in_is_monotonic(overlay):
    values = [overlay._alpha(t / 100) for t in range(0, 30)]
    assert values == sorted(values), "opacity must never dip during the ramp"


def test_timer_stops_when_idle(overlay):
    """An idle overlay must cost nothing; this is part of the CPU budget."""
    overlay.add_subtitle(_sub())
    overlay._lines[0].born = time.monotonic() - 99.0  # pretend it is ancient
    overlay._tick()
    assert overlay._lines == []
    assert not overlay._timer.isActive()


def test_voice_and_screen_use_different_colours(app):
    cfg = _cfg(screen_color="#e8f0ff", voice_color="#ffe9a8")
    widget = SubtitleOverlay(cfg, monitor=1)
    try:
        widget.add_subtitle(_sub("屏幕", Source.SCREEN))
        widget.add_subtitle(_sub("语音", Source.VOICE))
        assert widget._lines[0].color.name() == "#e8f0ff"
        assert widget._lines[1].color.name() == "#ffe9a8"
    finally:
        widget.close()


def test_paints_without_a_display(overlay):
    """paintEvent must not raise for any anchor / option combination."""
    for anchor in ("bottom-center", "top-center", "bottom-left", "center", "top-right"):
        overlay._cfg.anchor = anchor
        overlay.add_subtitle(_sub("测试字幕 test"))
        overlay.grab()
    overlay._cfg.show_source_text = True
    overlay._cfg.background = False
    overlay._cfg.outline = False
    overlay.grab()


def test_overlay_is_click_through_and_unfocusable(overlay):
    from PySide6.QtCore import Qt

    flags = overlay.windowFlags()
    assert flags & Qt.WindowType.WindowTransparentForInput
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert flags & Qt.WindowType.WindowStaysOnTopHint


# ------------------------------------------------------------------ bridging


def test_controller_delivers_from_a_worker_thread(app, overlay):
    """The pipeline publishes off the GUI thread; Qt must receive it safely."""
    bus = EventBus()
    controller = OverlayController(overlay, bus)
    try:
        thread = threading.Thread(target=lambda: bus.publish(_sub("来自线程")))
        thread.start()
        thread.join()

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not overlay._lines:
            QCoreApplication.processEvents()
        assert [line.text for line in overlay._lines] == ["来自线程"]
    finally:
        controller.close()


def test_controller_unsubscribes_on_close(overlay):
    bus = EventBus()
    controller = OverlayController(overlay, bus)
    controller.close()
    bus.publish(_sub("不应到达"))
    QCoreApplication.processEvents()
    assert overlay._lines == []


def test_controller_ignores_non_subtitle_events(overlay):
    bus = EventBus()
    controller = OverlayController(overlay, bus)
    try:
        bus.publish("not a subtitle")
        QCoreApplication.processEvents()
        assert overlay._lines == []
    finally:
        controller.close()


# -------------------------------------------------------------- roi anchoring


def _roi_overlay(app, anchor: str, roi=(0.1, 0.5, 0.4, 0.2)):
    cfg = _cfg(anchor=anchor, roi_gap_px=12, max_lines=2)
    widget = SubtitleOverlay(cfg, monitor=1, roi=roi)
    widget.setGeometry(0, 0, 1000, 800)
    return widget


def test_roi_below_places_the_stack_under_the_region(app):
    widget = _roi_overlay(app, "roi-below")
    try:
        roi = widget._roi_pixels()
        _left, top, _centred = widget._anchor_box(60)
        assert top == roi.bottom() + 12
    finally:
        widget.close()


def test_roi_above_places_the_stack_over_the_region(app):
    widget = _roi_overlay(app, "roi-above")
    try:
        roi = widget._roi_pixels()
        _left, top, _centred = widget._anchor_box(60)
        assert top == roi.top() - 12 - 60
    finally:
        widget.close()


def test_roi_over_centres_on_the_region(app):
    widget = _roi_overlay(app, "roi-over")
    try:
        roi = widget._roi_pixels()
        _left, top, _centred = widget._anchor_box(40)
        assert top == roi.center().y() - 20
    finally:
        widget.close()


def test_roi_anchor_stays_on_screen_when_the_region_is_at_the_bottom(app):
    widget = _roi_overlay(app, "roi-below", roi=(0.1, 0.85, 0.4, 0.14))
    try:
        _left, top, _centred = widget._anchor_box(200)
        assert top + 200 <= widget.height(), "stack must not run off the bottom"
        assert top >= widget._cfg.margin_px
    finally:
        widget.close()


def test_roi_anchor_falls_back_without_a_region(app):
    """If no ROI is configured, roi-* must behave like bottom-centre."""
    cfg = _cfg(anchor="roi-below", max_lines=2)
    widget = SubtitleOverlay(cfg, monitor=1, roi=None)
    widget.setGeometry(0, 0, 1000, 800)
    try:
        _left, top, _centred = widget._anchor_box(60)
        assert top == 800 - cfg.margin_px - 60
    finally:
        widget.close()


def test_slide_offset_decays_during_fade_in(app):
    widget = _roi_overlay(app, "roi-below")
    try:
        assert widget._slide_offset(0.0) == widget._cfg.slide_in_px
        assert widget._slide_offset(widget._fade_in) == 0
        assert widget._slide_offset(5.0) == 0
    finally:
        widget.close()


def test_each_region_anchors_to_its_own_roi(app):
    """Chat and kill feed sit in different corners; each stack must follow its own."""
    cfg = _cfg(anchor="roi-below", max_lines=2)
    rois = [(0.02, 0.55, 0.30, 0.20), (0.66, 0.04, 0.30, 0.20)]
    widget = SubtitleOverlay(cfg, monitor=1, rois=rois)
    widget.setGeometry(0, 0, 1000, 800)
    try:
        bottom = widget._roi_pixels(0)
        top = widget._roi_pixels(1)
        assert 400 < bottom.top() < 600, "region 0 should be lower on screen"
        assert top.top() < 100, "region 1 should be near the top"

        _l0, top0, _c0 = widget._anchor_box(40, region=0)
        _l1, top1, _c1 = widget._anchor_box(40, region=1)
        assert top0 == bottom.bottom() + cfg.roi_gap_px
        assert top1 == top.bottom() + cfg.roi_gap_px
        assert top0 > top1
    finally:
        widget.close()


def test_subtitle_region_index_selects_the_anchor(app):
    cfg = _cfg(anchor="roi-below", max_lines=2)
    widget = SubtitleOverlay(cfg, monitor=1, rois=[(0.0, 0.7, 0.3, 0.2), (0.7, 0.0, 0.3, 0.2)])
    widget.setGeometry(0, 0, 1000, 800)
    try:
        widget.add_subtitle(Subtitle("s", "底部", Source.SCREEN, Lang.EN, region=0))
        widget.add_subtitle(Subtitle("s", "顶部", Source.VOICE, Lang.EN, region=1))
        assert [line.region for line in widget._lines] == [0, 1]
        widget.grab()  # must paint two separate stacks without raising
    finally:
        widget.close()


def test_unknown_region_index_falls_back_to_the_first_roi(app):
    cfg = _cfg(anchor="roi-below")
    widget = SubtitleOverlay(cfg, monitor=1, rois=[(0.1, 0.5, 0.3, 0.2)])
    widget.setGeometry(0, 0, 1000, 800)
    try:
        assert widget._roi_pixels(99) == widget._roi_pixels(0)
    finally:
        widget.close()


def test_reassert_top_is_a_noop_off_windows(app):
    """Regression: Qt's raise_() pushes a topmost Tool window into the normal
    layer on X11, which pushed our subtitles *behind* other topmost windows.
    Verified live: with the timer on, subtitles vanished; off, they returned."""
    import sys

    cfg = _cfg(keep_on_top=True)
    widget = SubtitleOverlay(cfg, monitor=1)
    try:
        raised: list[int] = []
        widget.raise_ = lambda: raised.append(1)  # type: ignore[method-assign]
        widget._reassert_top()
        if sys.platform != "win32":
            assert raised == [], "must not call raise_() on non-Windows"
    finally:
        widget.close()


def test_keep_on_top_starts_and_stops_with_visibility(app):
    cfg = _cfg(keep_on_top=True)
    widget = SubtitleOverlay(cfg, monitor=1)
    try:
        assert widget._on_top_timer is not None
        widget.show()
        app.processEvents()
        assert widget._on_top_timer.isActive()
        widget.hide()
        app.processEvents()
        assert not widget._on_top_timer.isActive(), "timer must stop when hidden"
    finally:
        widget.close()


def test_keep_on_top_can_be_disabled(app):
    widget = SubtitleOverlay(_cfg(keep_on_top=False), monitor=1)
    try:
        assert widget._on_top_timer is None
    finally:
        widget.close()


def test_roi_anchor_centres_on_the_region_not_the_wrap_width(app):
    """Regression: with a narrow chat box the stack was centred inside the full
    wrap width, landing hundreds of px away from the box it describes."""
    cfg = _cfg(anchor="roi-below", max_width_ratio=0.62)
    rois = [(0.04, 0.70, 0.21, 0.13)]  # narrow box near the left edge
    widget = SubtitleOverlay(cfg, monitor=1, rois=rois)
    widget.setGeometry(0, 0, 1920, 1080)
    try:
        roi = widget._roi_pixels(0)
        _left, _top, centre_x = widget._anchor_box(60, region=0)
        assert centre_x == roi.center().x()
        # The old bug put it near the middle of the screen instead.
        assert abs(centre_x - 1920 // 2) > 200
    finally:
        widget.close()


def test_screen_centre_anchor_uses_the_screen_middle(app):
    cfg = _cfg(anchor="bottom-center")
    widget = SubtitleOverlay(cfg, monitor=1)
    widget.setGeometry(0, 0, 1000, 800)
    try:
        _left, _top, centre_x = widget._anchor_box(40)
        assert centre_x == 500
    finally:
        widget.close()


def test_left_anchor_is_not_centred(app):
    cfg = _cfg(anchor="bottom-left")
    widget = SubtitleOverlay(cfg, monitor=1)
    widget.setGeometry(0, 0, 1000, 800)
    try:
        left, _top, centre_x = widget._anchor_box(40)
        assert centre_x is None
        assert left == cfg.margin_px
    finally:
        widget.close()
