from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from fpst.capture.gate import ChangedOnly
from fpst.core.bus import EventBus
from fpst.core.dedupe import RecentTexts, normalise
from fpst.core.interfaces import ScreenSource
from fpst.core.latest import LatestValue
from fpst.core.types import Frame, Subtitle
from fpst.ocr.mock import MockOCREngine
from fpst.pipeline import ScreenPipeline
from fpst.translate.mock import MockTranslator

# ---------------------------------------------------------------- LatestValue


def test_latest_value_overwrites_instead_of_queueing():
    box: LatestValue[int] = LatestValue()
    for i in range(10):
        box.put(i)
    assert box.get(timeout=0.01) == (10, 9)  # newest, not oldest
    assert box.stats["dropped"] == 9


def test_latest_value_get_timeout():
    box: LatestValue[int] = LatestValue()
    assert box.get(timeout=0.01) is None


def test_latest_value_close_wakes_consumer():
    box: LatestValue[int] = LatestValue()

    def closer():
        time.sleep(0.02)
        box.close()

    threading.Thread(target=closer, daemon=True).start()
    started = time.monotonic()
    assert box.get(timeout=2.0) is None
    assert time.monotonic() - started < 1.0


# -------------------------------------------------------------------- dedupe


def test_recent_texts_rejects_repeats():
    seen = RecentTexts()
    assert seen.check_and_add("gg wp") is True
    assert seen.check_and_add("gg wp") is False
    assert seen.check_and_add("GG   WP") is False  # whitespace + case normalised


def test_recent_texts_expires_after_ttl():
    seen = RecentTexts(ttl=10.0)
    assert seen.check_and_add("gg", now=100.0) is True
    assert seen.check_and_add("gg", now=105.0) is False
    assert seen.check_and_add("gg", now=115.0) is True  # 15s later -> expired


def test_recent_texts_respects_max_entries():
    seen = RecentTexts(max_entries=3)
    for i in range(5):
        seen.check_and_add(f"line {i}")
    assert len(seen) == 3
    assert "line 0" not in seen


def test_normalise_collapses_ocr_jitter():
    assert normalise("  GG   WP \n") == "gg wp"


# ------------------------------------------------------------------ pipeline


class ReplaySource(ScreenSource):
    """Feeds a fixed script of frames, holding the last one (like a static screen)."""

    name = "replay"

    def __init__(self, images: list[np.ndarray]) -> None:
        self._images = images
        self._i = 0

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def grab(self) -> Frame | None:
        img = self._images[min(self._i, len(self._images) - 1)]
        self._i += 1
        return Frame(image=img)

    @property
    def size(self) -> tuple[int, int]:
        h, w = self._images[0].shape[:2]
        return int(w), int(h)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run(pipeline: ScreenPipeline, until, timeout: float = 5.0):
    pipeline.start()
    try:
        return wait_until(until, timeout)
    finally:
        pipeline.stop()


def test_pipeline_translates_each_line_once():
    """The screen keeps showing the same 3 lines; translation must fire 3 times total."""
    image = np.zeros((60, 200, 3), np.uint8)
    bus = EventBus()
    got: list[Subtitle] = []
    bus.subscribe(Subtitle, got.append)

    ocr = MockOCREngine(lines=["gg wp", "rush b", "nice shot"])
    translator = MockTranslator()
    pipeline = ScreenPipeline(
        ReplaySource([image]),
        ocr,
        translator,
        bus,
        max_fps=50,
        merge_lines=False,
        dedupe_ttl=60,
    )

    pipeline.start()
    try:
        assert wait_until(lambda: len(got) >= 3)
        time.sleep(0.3)
    finally:
        pipeline.stop()  # keep scanning: a runaway loop would produce duplicates
    assert [s.dst_text for s in got] == ["⟦zh⟧gg wp", "⟦zh⟧rush b", "⟦zh⟧nice shot"]
    assert ocr.calls > 1, "pipeline should keep scanning"
    assert sum(len(c) for c in translator.calls) == 3, "each line translated exactly once"


def test_pipeline_reports_latency_and_source():
    image = np.zeros((60, 200, 3), np.uint8)
    bus = EventBus()
    got: list[Subtitle] = []
    bus.subscribe(Subtitle, got.append)

    pipeline = ScreenPipeline(
        ReplaySource([image]),
        MockOCREngine(lines=["gg"]),
        MockTranslator(),
        bus,
        max_fps=50,
        merge_lines=False,
    )
    assert _run(pipeline, lambda: len(got) >= 1)
    pipeline.stop()

    assert got[0].src_text == "gg"
    assert got[0].source.value == "screen"
    assert got[0].latency_ms >= 0


def test_pipeline_ignores_frames_when_gate_says_unchanged():
    """With the change gate in front, a static screen must cost one OCR run only."""
    image = np.zeros((60, 200, 3), np.uint8)
    bus = EventBus()
    ocr = MockOCREngine(lines=["gg"])
    pipeline = ScreenPipeline(
        ChangedOnly(ReplaySource([image]), min_change_ratio=0.001),
        ocr,
        MockTranslator(),
        bus,
        max_fps=50,
        merge_lines=False,
    )
    assert _run(pipeline, lambda: ocr.calls >= 1)
    time.sleep(0.3)
    pipeline.stop()
    assert ocr.calls == 1, f"expected a single OCR run, got {ocr.calls}"


def test_pipeline_survives_ocr_exceptions():
    class BoomOCR(MockOCREngine):
        def recognize(self, frame):
            raise RuntimeError("engine exploded")

    image = np.zeros((60, 200, 3), np.uint8)
    pipeline = ScreenPipeline(
        ReplaySource([image]),
        BoomOCR(),
        MockTranslator(),
        EventBus(),
        max_fps=50,
        merge_lines=False,
    )
    pipeline.start()
    try:
        assert wait_until(lambda: pipeline.stats.errors >= 2, timeout=3.0)
    finally:
        pipeline.stop()
    # The worker thread must still be alive rather than dead after the first error.


@pytest.mark.slow
def test_pipeline_with_real_ocr_end_to_end():
    """Two messages, then a third appears -- only the new one should be translated."""
    pytest.importorskip("rapidocr_onnxruntime")
    from pathlib import Path

    from PIL import Image, ImageDraw, ImageFont

    from fpst.ocr.rapidocr_engine import RapidOCREngine

    font_path = next(
        (
            p
            for p in (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
            )
            if Path(p).exists()
        ),
        None,
    )
    if font_path is None:
        pytest.skip("no TrueType font")

    def render(lines: list[str]) -> np.ndarray:
        img = Image.new("RGB", (520, 40 + 34 * len(lines)), (18, 20, 24))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(font_path, 22)
        for i, line in enumerate(lines):
            draw.text((12, 10 + i * 34), line, font=font, fill=(235, 235, 235))
        return np.array(img)[:, :, ::-1]

    first = render(["PlayerOne: rush b now", "TeamMate: enemy is low"])
    second = render(["PlayerOne: rush b now", "TeamMate: enemy is low", "PlayerTwo: rotating mid"])

    bus = EventBus()
    got: list[Subtitle] = []
    bus.subscribe(Subtitle, got.append)
    translator = MockTranslator()

    pipeline = ScreenPipeline(
        ReplaySource([first, second]),
        RapidOCREngine(lang="en"),
        translator,
        bus,
        max_fps=20,
        merge_lines=False,
        dedupe_ttl=60,
    )
    assert _run(pipeline, lambda: len(got) >= 3, timeout=25.0)
    pipeline.stop()

    translated = [t for call in translator.calls for t in call]
    assert sum("rush" in t for t in translated) == 1, "first line must not be re-translated"
    assert sum("rotating" in t for t in translated) == 1, "new line must be translated"
    assert len({s.src_text for s in got}) == 3


# ------------------------------------------------------------------ debounce


class ChurningSource(ScreenSource):
    """A source whose image never settles -- like a scroll or fade animation."""

    name = "churn"

    def __init__(self) -> None:
        self.n = 0

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def grab(self) -> Frame | None:
        self.n += 1
        img = np.zeros((40, 80, 3), np.uint8)
        img[:, :, 0] = self.n % 256  # every frame differs
        return Frame(image=img)

    @property
    def size(self) -> tuple[int, int]:
        return (80, 40)


def test_debounce_still_fires_when_screen_never_settles():
    """A continuously animating ROI must not starve OCR forever (starvation cap)."""
    ocr = MockOCREngine(lines=["gg"])
    pipeline = ScreenPipeline(
        ChurningSource(),
        ocr,
        MockTranslator(),
        EventBus(),
        max_fps=50,
        merge_lines=False,
        settle_s=0.4,  # never settles; cap is max(1.6, 1.4) = 1.6 s
    )
    pipeline.start()
    try:
        assert wait_until(lambda: ocr.calls >= 1, timeout=5.0), "starvation cap never fired"
    finally:
        pipeline.stop()


def test_debounce_suppresses_ocr_on_a_churning_screen():
    """Before the cap expires, a still-animating screen should produce no OCR runs."""
    ocr = MockOCREngine(lines=["gg"])
    pipeline = ScreenPipeline(
        ChurningSource(),
        ocr,
        MockTranslator(),
        EventBus(),
        max_fps=50,
        merge_lines=False,
        settle_s=0.5,
    )
    pipeline.start()
    try:
        time.sleep(0.3)  # cap is max(2.0, 1.5) = 2.0 s
        assert ocr.calls == 0, f"OCR ran {ocr.calls}x before the screen settled"
    finally:
        pipeline.stop()
