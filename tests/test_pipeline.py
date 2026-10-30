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


def test_skipped_translations_do_not_become_subtitles():
    """A noise line the model maps to "" must never reach the overlay."""

    class SkippingTranslator(MockTranslator):
        def translate(self, texts, src, dst):
            self.calls.append(list(texts))
            return ["" if t.startswith("+") else f"译:{t}" for t in texts]

    image = np.zeros((60, 200, 3), np.uint8)
    bus = EventBus()
    got: list[Subtitle] = []
    bus.subscribe(Subtitle, got.append)

    pipeline = ScreenPipeline(
        ReplaySource([image]),
        MockOCREngine(lines=["+150", "rush b", "PlayerOne"]),
        SkippingTranslator(),
        bus,
        max_fps=50,
        merge_lines=False,
    )
    pipeline.start()
    try:
        assert wait_until(lambda: len(got) >= 2)
        time.sleep(0.2)
    finally:
        pipeline.stop()

    assert [s.dst_text for s in got] == ["译:rush b", "译:PlayerOne"]


# --------------------------------------------------- translation isolation


def test_slow_translation_does_not_block_ocr():
    """A 20 s LLM call must not stop the OCR loop.

    Measured against the live DeepSeek API, the same prompt ranged from 0.8 s to
    28 s. Translation therefore runs on its own thread; if it shared the OCR
    thread, one sluggish request would freeze the subtitle feed.
    """
    import threading as _threading

    release = _threading.Event()

    class SlowTranslator(MockTranslator):
        def translate(self, texts, src, dst):
            release.wait(timeout=10)
            return [f"译:{t}" for t in texts]

    image = np.zeros((60, 200, 3), np.uint8)
    ocr = MockOCREngine(lines=["gg"])
    pipeline = ScreenPipeline(
        ReplaySource([image]),
        ocr,
        SlowTranslator(),
        EventBus(),
        max_fps=50,
        merge_lines=False,
    )
    pipeline.start()
    try:
        # OCR keeps running even though the single translation call is stuck.
        assert wait_until(lambda: ocr.calls >= 5, timeout=5.0), (
            f"OCR stalled behind translation: only {ocr.calls} runs"
        )
    finally:
        release.set()
        pipeline.stop()


def test_translation_runs_on_its_own_thread():
    seen: dict[str, str] = {}

    class ThreadRecorder(MockTranslator):
        def translate(self, texts, src, dst):
            import threading as _t

            seen["name"] = _t.current_thread().name
            return super().translate(texts, src, dst)

    image = np.zeros((60, 200, 3), np.uint8)
    pipeline = ScreenPipeline(
        ReplaySource([image]),
        MockOCREngine(lines=["gg"]),
        ThreadRecorder(),
        EventBus(),
        max_fps=50,
        merge_lines=False,
    )
    pipeline.start()
    try:
        assert wait_until(lambda: "name" in seen, timeout=5.0)
    finally:
        pipeline.stop()
    assert seen["name"] == "fpst-translate"


# ------------------------------------------------------------- multiple ROIs


def test_multiple_sources_each_produce_subtitles_with_their_region_index():
    """Chat bottom-left + kill feed top-right: both must reach the overlay, and
    each subtitle must remember which ROI it came from so it can be anchored."""
    image = np.zeros((60, 200, 3), np.uint8)
    bus = EventBus()
    got: list[Subtitle] = []
    bus.subscribe(Subtitle, got.append)

    class RegionOCR(MockOCREngine):
        """Reads different text depending on which region it was handed."""

        def __init__(self):
            super().__init__(lines=["x"])
            self.seen: list[int] = []

        def recognize(self, frame):
            idx = 0 if frame.image.shape[1] == 200 else 1
            self.seen.append(idx)
            self.calls += 1
            return MockOCREngine(lines=[f"region{idx} line"]).recognize(frame)

    pipeline = ScreenPipeline(
        [ReplaySource([image]), ReplaySource([np.zeros((60, 100, 3), np.uint8)])],
        RegionOCR(),
        MockTranslator(),
        bus,
        max_fps=50,
        merge_lines=False,
    )
    pipeline.start()
    try:
        assert wait_until(lambda: len(got) >= 2, timeout=5.0)
    finally:
        pipeline.stop()

    by_region = {s.region for s in got}
    assert by_region == {0, 1}, f"expected both regions, got {by_region}"
    assert {s.src_text for s in got} == {"region0 line", "region1 line"}


def test_simultaneous_changes_in_two_regions_are_both_processed():
    """Regression: a single-slot mailbox would let one region overwrite the other."""
    from fpst.core.latest import LatestPerKey

    box: LatestPerKey[int, str] = LatestPerKey()
    box.put(0, "chat")
    box.put(1, "killfeed")
    picked = {box.get(timeout=0.01), box.get(timeout=0.01)}
    assert picked == {(0, "chat"), (1, "killfeed")}


def test_one_region_changing_does_not_reprocess_another():
    """Each ROI has its own change gate, so a busy kill feed must not re-OCR chat."""
    quiet = np.zeros((40, 100, 3), np.uint8)
    busy = np.zeros((40, 100, 3), np.uint8)

    class CountingOCR(MockOCREngine):
        def recognize(self, frame):
            # The quiet region always yields identical pixels; the busy one ticks.
            if frame.image[0, 0, 0] == 0:
                self.calls += 1
                return MockOCREngine(lines=["quiet"]).recognize(frame)
            self.calls += 1
            return MockOCREngine(lines=[f"busy{frame.image[0, 0, 0]}"]).recognize(frame)

    class TickingSource(ReplaySource):
        def grab(self):
            self._i += 1
            img = busy.copy()
            img[0, 0, 0] = self._i % 250
            return Frame(image=img)

    ocr = CountingOCR()
    pipeline = ScreenPipeline(
        [
            ChangedOnly(ReplaySource([quiet]), min_change_ratio=0.001),
            ChangedOnly(TickingSource([quiet]), min_change_ratio=0.001),
        ],
        ocr,
        MockTranslator(),
        EventBus(),
        max_fps=50,
        merge_lines=False,
    )
    pipeline.start()
    try:
        time.sleep(0.6)
    finally:
        pipeline.stop()
    # The quiet region's gate must have suppressed repeats: total OCR runs stay
    # modest instead of doubling per tick.
    assert ocr.calls <= 8, f"quiet region was re-OCR'd too often ({ocr.calls} runs)"


def test_throttle_does_not_starve_regions_under_load():
    """Regression: the worker waited for the throttle window and then replaced
    its pending item with the newest one, so with many ROIs the same few regions
    were processed repeatedly while others never were."""

    class PerRegionOCR(MockOCREngine):
        def __init__(self):
            super().__init__(lines=["x"])
            self.seen: list[int] = []

        def recognize(self, frame):
            idx = frame.image[0, 0, 0]  # encoded by the source below
            self.seen.append(int(idx))
            self.calls += 1
            return MockOCREngine(lines=[f"region {idx}"]).recognize(frame)

    class TaggedSource(ScreenSource):
        name = "tagged"

        def __init__(self, tag: int) -> None:
            self.tag = tag

        def open(self):
            pass

        def close(self):
            pass

        def grab(self):
            img = np.zeros((40, 60, 3), np.uint8)
            img[0, 0, 0] = self.tag
            return Frame(image=img)

        @property
        def size(self):
            return (60, 40)

    ocr = PerRegionOCR()
    pipeline = ScreenPipeline(
        [TaggedSource(i) for i in range(8)],
        ocr,
        MockTranslator(),
        EventBus(),
        max_fps=50,  # sources change constantly
        min_interval=0.05,  # heavy throttle relative to the frame rate
        merge_lines=False,
    )
    pipeline.start()
    try:
        time.sleep(1.5)
    finally:
        pipeline.stop()

    distinct = set(ocr.seen)
    assert len(distinct) >= 5, f"throttle starved regions; only saw {sorted(distinct)}"


def test_identical_text_in_two_regions_is_not_deduped():
    """Two teammates can say the same thing; a shared cache would drop the second."""

    class EchoOCR(MockOCREngine):
        def recognize(self, frame):
            self.calls += 1
            idx = frame.image[0, 0, 0]
            return MockOCREngine(lines=[f"nice shot {idx}"]).recognize(frame)

    class Tagged(ScreenSource):
        name = "tagged"

        def __init__(self, tag):
            self.tag = tag

        def open(self):
            pass

        def close(self):
            pass

        def grab(self):
            img = np.zeros((40, 60, 3), np.uint8)
            img[0, 0, 0] = self.tag
            return Frame(image=img)

        @property
        def size(self):
            return (60, 40)

    class SameOCR(MockOCREngine):
        """Both regions report the *identical* message."""

        def recognize(self, frame):
            self.calls += 1
            return MockOCREngine(lines=["nice shot"]).recognize(frame)

    bus = EventBus()
    got: list[Subtitle] = []
    bus.subscribe(Subtitle, got.append)
    pipeline = ScreenPipeline(
        [Tagged(1), Tagged(2)], SameOCR(), MockTranslator(), bus, max_fps=50, merge_lines=False
    )
    pipeline.start()
    try:
        assert wait_until(lambda: {s.region for s in got} == {0, 1}, timeout=5.0)
    finally:
        pipeline.stop()


def test_bare_player_name_is_not_shown_as_a_subtitle():
    """OCR boxes the name separately; translating it produces a useless bubble."""
    from fpst.pipeline.screen import _is_bare_name

    assert _is_bare_name("xX_Shadow_Xx", "xX_Shadow_Xx") is True
    assert _is_bare_name("Echo", "Echo") is True
    assert _is_bare_name("Echo", "回声") is False, "a real translation must show"
    assert _is_bare_name("need cover", "need cover") is False, "has a space"
    assert _is_bare_name("gg", "gg") is False, "too short to be sure"
