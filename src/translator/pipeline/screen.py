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
from ..core.latest import LatestPerKey
from ..core.types import Frame, Lang, Source, Subtitle

log = logging.getLogger(__name__)

_MAX_LINES_PER_REQUEST = 40
"""Cap on lines sent in one translation request. More than this and the tail is
pushed back to the mailbox for the next round, keeping request size (and cost)
predictable however many regions are configured."""

__all__ = ["ScreenPipeline", "PipelineStats"]


@dataclass
class PipelineStats:
    frames_grabbed: int = 0
    frames_changed: int = 0
    frames_dropped: int = 0
    ocr_runs: int = 0
    ocr_space_ms: float = 0.0
    """Total idle time between OCR runs, used by bench to estimate duty cycle."""
    lines_seen: int = 0
    lines_new: int = 0
    lines_dropped: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)
    """Why lines were discarded, so the status panel can show what dominates."""
    errors: int = 0
    idle: bool = False
    frames_empty: int = 0
    """Grabs that returned nothing.

    A backend can be "working" and still hand back None every time: Desktop
    Duplication produces no frame while the screen is static. That is completely
    different from the change gate rejecting a frame, and the two need separate
    counters or the status panel cannot say which is happening."""
    frames_attempted: int = 0
    """Every grab() call, including the ones the change gate rejected.

    ``frames_grabbed`` only counts frames that passed the gate, so it cannot tell
    "the chat is not changing" apart from "capture is broken". This can."""
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
        source: ScreenSource | list[ScreenSource],
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
        settle_s: float = 0.0,
        filters: object | None = None,
    ) -> None:
        self._sources: list[ScreenSource] = (
            [source] if isinstance(source, ScreenSource) else list(source)
        )
        if not self._sources:
            raise ValueError("at least one screen source is required")
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
        # Anything with reason_for(text) -> str|None; a FilterChain normally.
        self._filters = filters
        self._min_interval = max(0.0, min_interval)
        # After this long with no change, poll the screen lazily instead of at
        # max_fps. 0 disables the idle mode.
        self._idle_timeout = max(0.0, idle_timeout)
        self._last_change = time.monotonic()
        # Debounce window: only hand a frame over once it has stopped changing.
        self._settle = max(0.0, settle_s)
        # Never let a continuously-animating screen starve OCR entirely.
        self._settle_cap = max(self._settle * 4, self._settle + 1.0)
        # One dedupe cache *per region*. A shared cache would treat two
        # teammates saying "nice shot" as a repeat and silently drop the second
        # one, which is exactly what happened with twelve chat panels.
        self._dedupe_ttl = dedupe_ttl
        self._dedupe: dict[int, RecentTexts] = {}
        # One pending slot *per region*: with several ROIs a single slot would let
        # one region's frame overwrite another's, silently losing a translation.
        self._mailbox: LatestPerKey[int, Frame] = LatestPerKey()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.capture_error = ""
        """Set when the capture thread could not start at all.

        Without it the UI keeps saying "running" while nothing is being captured,
        which is what made a missing bettercam look like "OCR count is 0".
        """
        # Translation runs on its own thread with its own capacity-1 mailbox.
        # A slow LLM (measured 0.8-28 s for the same prompt) must never block
        # OCR, or one sluggish request would freeze the subtitle feed entirely.
        self._translate_mailbox: LatestPerKey[int, list] = LatestPerKey()
        self.stats = PipelineStats()

    @classmethod
    def from_config(cls, cfg: AppConfig, bus: EventBus) -> ScreenPipeline:
        from ..capture import make_screen_sources
        from ..ocr import make_ocr_engine
        from ..translate import make_translator

        return cls(
            make_screen_sources(cfg.screen),
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
            settle_s=cfg.screen.settle_ms / 1000.0,
            filters=_build_filters(cfg.filters),
        )

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._capture_loop, name="translator-capture", daemon=True),
            threading.Thread(target=self._worker_loop, name="translator-ocr", daemon=True),
            threading.Thread(target=self._translate_loop, name="translator-translate", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._mailbox.close()
        self._translate_mailbox.close()
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

    @property
    def healthy(self) -> bool:
        """False when capture never started -- the UI must not claim "running"."""
        return not self.capture_error

    @property
    def translator_stats(self) -> dict[str, float]:
        """Cache/glossary counters, when the translator exposes them."""
        stats = getattr(self._translator, "stats", None)
        return dict(stats) if isinstance(stats, dict) else {}

    def __enter__(self) -> ScreenPipeline:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---------------------------------------------------------------- capture
    def _capture_loop(self) -> None:
        opened: list[ScreenSource] = []
        try:
            for source in self._sources:
                source.open()  # same thread as grab/close: DXGI requires it
                opened.append(source)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to open screen source")
            self.capture_error = f"{type(exc).__name__}: {exc}"
            for source in opened:
                with contextlib.suppress(Exception):
                    source.close()
            self._stop.set()
            return

        next_at = time.monotonic()
        self._pending: dict[int, Frame] = {}
        self._last_change_at: dict[int, float] = {}
        self._first_change_at: dict[int, float] = {}
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                for index, source in enumerate(self._sources):
                    self.stats.frames_attempted += 1
                    try:
                        frame = source.grab()
                    except Exception:  # noqa: BLE001
                        self.stats.errors += 1
                        log.exception("grab failed for region %d", index)
                        continue

                    if frame is None:
                        self.stats.frames_empty += 1
                    if frame is not None:
                        self.stats.frames_grabbed += 1
                        self.stats.frames_changed += 1
                        self._last_change = now
                        self._pending[index] = frame
                        self._last_change_at[index] = now
                        self._first_change_at.setdefault(index, now)

                # Debounce per region: only release once *that* ROI holds still,
                # so we OCR finished text instead of a fade-in animation.
                for index in list(self._pending):
                    last = self._last_change_at.get(index, now)
                    first = self._first_change_at.get(index, now)
                    starved = now - first >= self._settle_cap
                    if now - last >= self._settle or starved:
                        self._mailbox.put(index, self._pending.pop(index))
                        self._last_change_at.pop(index, None)
                        self._first_change_at.pop(index, None)

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
            for source in opened:
                with contextlib.suppress(Exception):
                    source.close()

    # ----------------------------------------------------------------- worker
    def _worker_loop(self) -> None:
        try:
            self._ocr.warmup()
        except Exception:  # noqa: BLE001
            self.stats.errors += 1
            log.exception("OCR warmup failed")

        # Throttle recognition per region, not globally.
        #
        # A single global "last run" timestamp caps throughput at
        # 1/min_interval no matter how many ROIs are waiting, so with twelve
        # regions every one of them waits its turn in a queue: measured
        # per-region latency was 1x and 2x min_interval, and a full sweep took
        # N x interval. Each region now has its own deadline, so a quiet region
        # is recognised immediately while a chatty one still cannot exceed its
        # own budget.
        last_ocr: dict[int, float] = {}
        while not self._stop.is_set():
            now = time.monotonic()
            pending = self._mailbox.pending_keys()
            due = [key for key in pending if now - last_ocr.get(key, 0.0) >= self._min_interval]

            if not due:
                if pending:
                    soonest = min(last_ocr.get(key, 0.0) + self._min_interval for key in pending)
                    self._stop.wait(min(0.2, max(0.01, soonest - now)))
                else:
                    self._mailbox.wait_for_data(0.2)
                continue

            key = due[0]
            frame = self._mailbox.take(key)
            if frame is None:
                continue
            last_ocr[key] = time.monotonic()
            try:
                self._process(key, frame)
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                log.exception("OCR/translate failed")

    def _translate_loop(self) -> None:
        """Translate every pending region in one request.

        Per-region requests serialise badly: twelve chat boxes meant twelve
        round trips, so the last region waited ~9 s for its first subtitle while
        the first appeared immediately. Our Translator is already a batch
        interface, so draining all pending regions into a single call collapses
        that to one round trip (and costs less, which matters for Baidu's 1 QPS).
        """
        while not self._stop.is_set():
            batches: list[tuple[int, list]] = []
            while True:
                got = self._translate_mailbox.get(timeout=0.15 if not batches else 0)
                if got is None:
                    break
                batches.append(got)
            if not batches:
                continue

            # One flat list across regions; results map back by position.
            items: list[tuple[int, object, float]] = []
            for region_index, region_items in batches:
                for region, frame_ts in region_items:
                    items.append((region_index, region, frame_ts))

            overflow = items[_MAX_LINES_PER_REQUEST:]
            if overflow:
                # Put the tail back for the next round rather than growing the
                # request without bound.
                by_region: dict[int, list] = {}
                for region_index, region, frame_ts in overflow:
                    by_region.setdefault(region_index, []).append((region, frame_ts))
                for region_index, region_items in by_region.items():
                    self._translate_mailbox.extend(region_index, region_items)
                items = items[:_MAX_LINES_PER_REQUEST]

            started = time.perf_counter()
            try:
                translated = self._translator.translate(
                    [region.text for _, region, _ in items], self._src_lang, self._dst_lang
                )
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                log.exception("translate failed")
                continue
            elapsed = (time.perf_counter() - started) * 1000
            self.stats._tr_ms.append(elapsed)

            for (region_index, region, frame_ts), dst in zip(items, translated, strict=False):
                if not dst:
                    continue  # model said "nothing to show"
                if _is_bare_name(region.text, dst):
                    continue  # a lone player name is noise, not a subtitle
                self._bus.publish(
                    Subtitle(
                        src_text=region.text,
                        dst_text=dst,
                        source=Source.SCREEN,
                        src_lang=region.lang,
                        ts=region.ts,
                        confidence=region.confidence,
                        region=region_index,
                        latency_ms=(time.time() - frame_ts) * 1000 + elapsed,
                    )
                )

    def _process(self, region_index: int, frame: Frame) -> None:
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

        # Drop non-messages before spending anything on them: a filtered line
        # costs no translation request at all.
        if self._filters is not None:
            kept = []
            for region in regions:
                reason = self._filters.reason_for(region.text)  # type: ignore[attr-defined]
                if reason:
                    self.stats.lines_dropped += 1
                    self.stats.drop_reasons[reason] = self.stats.drop_reasons.get(reason, 0) + 1
                else:
                    kept.append(region)
            regions = kept

        regions = regions[: self._max_lines]

        # Only pay for translation on genuinely new lines, per region.
        seen = self._dedupe.setdefault(region_index, RecentTexts(ttl=self._dedupe_ttl))
        fresh = [r for r in regions if r.text and seen.check_and_add(r.text)]
        self.stats.lines_new += len(fresh)
        if not fresh:
            return

        # Hand off. The translation thread owns the network call so a slow
        # request can never stall OCR. Batches are *merged per region*: a plain
        # latest-wins slot would drop a region's lines when two ROIs change at
        # the same moment.
        self._translate_mailbox.extend(region_index, [(region, frame.ts) for region in fresh])


def _build_filters(cfg) -> object | None:
    """Build the filter chain, or None when every rule is disabled."""
    from ..ocr.filters import FilterChain

    chain = FilterChain(cfg)
    if (
        cfg.min_chars <= 1
        and not cfg.require_regex
        and not cfg.extra_drop_regex
        and not cfg.keep_regex
        and not cfg.drop_pure_symbols
        and not cfg.drop_repeated_chars
        and cfg.min_letters <= 0
    ):
        return None
    return chain


def _is_bare_name(source: str, translation: str) -> bool:
    """True for a line that is only a player name (e.g. "xX_Shadow_Xx").

    OCR often boxes the chat name separately from the message, and translating a
    lone name produces a useless subtitle bubble. Treated as noise only when the
    model returned it unchanged and it looks like an identifier: one token, ASCII,
    at least three characters.
    """
    text = source.strip()
    if text != translation.strip():
        return False
    if " " in text or len(text) < 3:
        return False
    return all(ord(ch) < 128 for ch in text)


def _lang(value: str) -> Lang:
    try:
        return Lang(value)
    except ValueError:
        return Lang.AUTO
