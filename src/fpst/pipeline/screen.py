"""The always-on screen pipeline.

Two threads, one mailbox:

    capture thread            mailbox (cap 1)         worker thread
    ----------------          ---------------         -------------
    grab -> ChangedOnly  -->  [newest frame]  -->  OCR -> dedupe -> translate
                                                                      |
                                                                 EventBus

Why two threads and not one: grabbing is cheap and must stay on time (a 60 Hz
DXGI poll costs ~2% CPU), while OCR is expensive (400 ms) and would otherwise
block capture, guaranteeing we always analyse a stale frame. Decoupling them
means capture never waits, and the mailbox guarantees the worker always starts
on the *latest* frame.

Thread affinity: ``ScreenSource.open/close`` are called **inside** the capture
thread. DXGI's COM objects are thread-bound; opening on the main thread and
grabbing here would fail or silently return nothing.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..core.bus import EventBus
from ..core.config import AppConfig
from ..core.dedupe import RecentTexts
from ..core.interfaces import OCREngine, ScreenSource, Translator
from ..core.latest import LatestValue
from ..core.types import Frame, Lang, Source, Subtitle

log = logging.getLogger(__name__)

__all__ = ["ScreenPipeline", "PipelineStats"]


@dataclass
class PipelineStats:
    frames_grabbed: int = 0
    frames_changed: int = 0
    frames_dropped: int = 0
    ocr_runs: int = 0
    lines_seen: int = 0
    lines_new: int = 0
    errors: int = 0
    idle: bool = False
    _ocr_ms: deque[float] = field(default_factory=lambda: deque(maxlen=50))
    _tr_ms: deque[float] = field(default_factory=lambda: deque(maxlen=50))

    def _avg(self, values: deque[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @property
    def ocr_ms(self) -> float:
        return self._avg(self._ocr_ms)

    @property
    def translate_ms(self) -> float:
        return self._tr_ms[-1] if self._tr_ms else 0.0

    def summary(self) -> str:
        return (
            f"grabbed={self.frames_grabbed} changed={self.frames_changed} "
            f"dropped={self.frames_dropped} ocr={self.ocr_runs}@{self.ocr_ms:.0f}ms "
            f"new={self.lines_new}/{self.lines_seen} tr={self.translate_ms:.0f}ms"
        )


class ScreenPipeline:
    def __init__(
        self,
        source: ScreenSource,
        ocr: OCREngine,
        translator: Translator,
        bus: EventBus,
        *,
        max_fps: float = 4.0,
        src_lang: Lang = Lang.EN,
        dst_lang: Lang = Lang.ZH,
        merge_lines: bool = True,
        min_confidence: float = 0.0,
        dedupe_ttl: float = 90.0,
        max_lines: int = 12,
        min_interval: float = 0.0,
        idle_timeout: float = 0.0,
    ) -> None:
        self._source = source
        self._ocr = ocr
        self._translator = translator
        self._bus = bus
        self._period = 1.0 / max(0.2, max_fps)
        self._src_lang = src_lang
        self._dst_lang = dst_lang
        self._merge = merge_lines
        self._min_conf = min_confidence
        self._max_lines = max_lines
        # Minimum seconds between OCR runs. Capture stays fast (it is cheap);
        # recognition is the expensive stage and gets spaced out.
        self._min_interval = max(0.0, min_interval)
        # After this long with no change, poll the screen lazily instead of at
        # max_fps. 0 disables the idle mode.
        self._idle_timeout = max(0.0, idle_timeout)
        self._last_change = time.monotonic()
        self._dedupe = RecentTexts(ttl=dedupe_ttl)
        self._mailbox: LatestValue[Frame] = LatestValue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.stats = PipelineStats()

    @classmethod
    def from_config(cls, cfg: AppConfig, bus: EventBus) -> ScreenPipeline:
        from ..capture import make_screen_source
        from ..ocr import make_ocr_engine
        from ..translate import make_translator

        return cls(
            make_screen_source(cfg.screen),
            make_ocr_engine(cfg.ocr),
            make_translator(cfg.translate),
            bus,
            max_fps=cfg.screen.max_fps,
            src_lang=_lang(cfg.translate.src_lang),
            dst_lang=_lang(cfg.translate.dst_lang),
            merge_lines=cfg.ocr.merge_lines,
            min_confidence=cfg.ocr.min_confidence,
            max_lines=cfg.ocr.max_lines,
            min_interval=cfg.performance.ocr_min_interval,
            idle_timeout=cfg.performance.idle_timeout,
        )

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._capture_loop, name="fpst-capture", daemon=True),
            threading.Thread(target=self._worker_loop, name="fpst-ocr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._mailbox.close()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def __enter__(self) -> ScreenPipeline:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---------------------------------------------------------------- capture
    def _capture_loop(self) -> None:
        try:
            self._source.open()  # same thread as grab/close: DXGI requires it
        except Exception:  # noqa: BLE001
            log.exception("failed to open screen source")
            self._stop.set()
            return

        next_at = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    frame = self._source.grab()
                except Exception:  # noqa: BLE001
                    self.stats.errors += 1
                    log.exception("grab failed")
                    frame = None
                if frame is not None:
                    self.stats.frames_grabbed += 1
                    self.stats.frames_changed += 1
                    self._last_change = time.monotonic()
                    self._mailbox.put(frame)

                # Idle back-off: nothing has changed for a while, so there is
                # nothing to find -- poll at 1 Hz instead of max_fps.
                period = self._period
                if self._idle_timeout and (
                    time.monotonic() - self._last_change > self._idle_timeout
                ):
                    period = max(period, 1.0)
                    self.stats.idle = True
                else:
                    self.stats.idle = False

                next_at += period
                delay = next_at - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_at = time.monotonic()  # we fell behind; don't sprint to catch up
        finally:
            with contextlib.suppress(Exception):
                self._source.close()

    # ----------------------------------------------------------------- worker
    def _worker_loop(self) -> None:
        try:
            self._ocr.warmup()
        except Exception:  # noqa: BLE001
            self.stats.errors += 1
            log.exception("OCR warmup failed")

        last_run = 0.0
        while not self._stop.is_set():
            got = self._mailbox.get(timeout=0.2)
            if got is None:
                continue

            # Throttle recognition, not capture. Waiting *before* OCR means the
            # frame we finally process is the freshest one, not a backlog.
            gap = self._min_interval - (time.monotonic() - last_run)
            if gap > 0:
                if self._stop.wait(gap):
                    break
                newer = self._mailbox.get(timeout=0)
                if newer is not None:
                    got = newer

            last_run = time.monotonic()
            try:
                self._process(got[1])
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                log.exception("OCR/translate failed")

    def _process(self, frame: Frame) -> None:
        started = time.perf_counter()
        regions = self._ocr.recognize(frame)
        self.stats.ocr_runs += 1
        self.stats._ocr_ms.append((time.perf_counter() - started) * 1000)
        self.stats.lines_seen += len(regions)

        if regions and self._merge:
            from ..ocr import postprocess

            regions = postprocess(regions, merge=True)

        if self._min_conf > 0:
            regions = [r for r in regions if r.confidence >= self._min_conf]
        regions = regions[: self._max_lines]

        # Only pay for translation on genuinely new lines.
        fresh = [r for r in regions if r.text and self._dedupe.check_and_add(r.text)]
        self.stats.lines_new += len(fresh)
        if not fresh:
            return

        tr_started = time.perf_counter()
        try:
            translated = self._translator.translate(
                [r.text for r in fresh], self._src_lang, self._dst_lang
            )
        except Exception:  # noqa: BLE001
            self.stats.errors += 1
            log.exception("translate failed")
            return
        tr_elapsed = (time.perf_counter() - tr_started) * 1000
        self.stats._tr_ms.append(tr_elapsed)

        for region, dst in zip(fresh, translated, strict=False):
            if not dst:
                continue
            self._bus.publish(
                Subtitle(
                    src_text=region.text,
                    dst_text=dst,
                    source=Source.SCREEN,
                    src_lang=region.lang,
                    ts=region.ts,
                    confidence=region.confidence,
                    latency_ms=(time.time() - frame.ts) * 1000 + tr_elapsed,
                )
            )


def _lang(value: str) -> Lang:
    try:
        return Lang(value)
    except ValueError:
        return Lang.AUTO
