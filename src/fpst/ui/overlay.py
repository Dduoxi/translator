"""The transparent subtitle overlay.

Design constraints that shape this file:

* **Click-through and never focusable.** It floats over a game. If it steals a
  click or the keyboard, it is worse than useless.
* **Invisible to screen capture (Windows).** Otherwise our own Chinese subtitle
  is captured by the OCR stage, translated again, and shown again -- an infinite
  feedback loop that also burns API credit. ``SetWindowDisplayAffinity`` with
  ``WDA_EXCLUDEFROMCAPTURE`` removes the window from every capture API.
* **Cheap when idle.** Repaints run on a 10 Hz timer that stops itself once
  every line has expired, so an idle overlay costs nothing. This is part of the
  50%-of-one-core budget.

Threading: subtitles arrive on the pipeline worker thread. Qt widgets may only
be touched on the GUI thread, so :class:`OverlayController` bridges them with a
queued signal connection.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from ..core.bus import EventBus
from ..core.config import UIConfig
from ..core.types import Source, Subtitle

log = logging.getLogger(__name__)

__all__ = ["SubtitleOverlay", "OverlayController", "create_app"]

FONT_STACK = [
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "Sans Serif",
]

_TICK_MS = 100
"""Repaint cadence while subtitles are on screen (10 Hz, for the fade)."""


@dataclass
class _Line:
    text: str
    source_text: str
    color: QColor
    born: float  # time.monotonic()
    latency_ms: float
    region: int = 0


def _exclude_from_capture(window: QWidget) -> str:
    """Windows: keep the overlay out of screen captures. Best effort."""
    if sys.platform != "win32":
        return "unsupported"
    try:
        import ctypes

        hwnd = int(window.winId())
        wda_exclude_from_capture = 0x00000011
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        ok = user32.SetWindowDisplayAffinity(hwnd, wda_exclude_from_capture)
        return "excluded" if ok else "failed"
    except Exception as exc:  # noqa: BLE001
        log.debug("could not exclude overlay from capture: %s", exc)
        return "failed"


class SubtitleOverlay(QWidget):
    """Full-monitor, transparent, click-through subtitle canvas."""

    def __init__(
        self,
        cfg: UIConfig,
        monitor: int = 1,
        roi: tuple[float, float, float, float] | None = None,
        rois: list[tuple[float, float, float, float] | None] | None = None,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._cfg = cfg
        if rois is None:
            rois = [roi]
        self._rois_rel = rois
        """Scanned regions as monitor fractions; a subtitle anchors to its own."""
        self._lines: list[_Line] = []
        self._ttl = max(1.0, cfg.line_ttl)
        self._fade = min(1.5, self._ttl / 2)
        self._fade_in = max(0.0, cfg.fade_in_ms / 1000.0)
        self._slide = max(0, cfg.slide_in_px)
        self._screen_color = QColor(cfg.screen_color)
        self._voice_color = QColor(cfg.voice_color)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("fpst overlay")

        screen = self._pick_screen(monitor)
        self.setGeometry(screen.geometry())

        self._font = self._build_font(QGuiApplication.primaryScreen())
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _pick_screen(monitor: int):
        screens = QGuiApplication.screens()
        index = max(0, min(monitor - 1, len(screens) - 1))
        return screens[index]

    def _build_font(self, screen) -> QFont:
        from PySide6.QtGui import QFontDatabase

        available = set(QFontDatabase.families())
        family = self._cfg.font_family
        if not family or family not in available:
            family = next((f for f in FONT_STACK if f in available), "Sans Serif")
        dpi = float(screen.devicePixelRatio()) if screen else 1.0
        font = QFont(family)
        font.setPixelSize(max(10, int(self._cfg.font_size * dpi)))
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        affinity = _exclude_from_capture(self)
        log.info(
            "overlay shown on %dx%d, capture affinity=%s", self.width(), self.height(), affinity
        )

    # ------------------------------------------------------------------- data
    def add_subtitle(self, subtitle: Subtitle) -> None:
        """Must be called on the GUI thread (OverlayController guarantees it)."""
        text = subtitle.dst_text.strip()
        if not text:
            return
        base = self._voice_color if subtitle.source is Source.VOICE else self._screen_color
        color = QColor(base)
        if subtitle.confidence < 0.7:
            color.setAlpha(200)

        self._lines.append(
            _Line(
                text=text,
                source_text=subtitle.src_text,
                color=color,
                born=time.monotonic(),
                latency_ms=subtitle.latency_ms,
                region=subtitle.region,
            )
        )
        if len(self._lines) > self._cfg.max_lines * 3:
            del self._lines[: len(self._lines) - self._cfg.max_lines * 3]

        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def clear(self) -> None:
        self._lines.clear()
        self._timer.stop()
        self.update()

    # ------------------------------------------------------------------ paint
    def _alive(self, now: float) -> list[tuple[_Line, float]]:
        alive = [(line, now - line.born) for line in self._lines if now - line.born < self._ttl]
        return alive[-self._cfg.max_lines :]

    def _tick(self) -> None:
        if not self._alive(time.monotonic()):
            self._lines.clear()
            self._timer.stop()  # idle: zero cost until the next subtitle
        self.update()

    def _alpha(self, age: float) -> float:
        """0 -> 1 over ``fade_in``, hold, then ease out over ``fade``.

        The ramp-in matters: a hard cut reads as a glitch while you are playing,
        a 200 ms fade reads as something the game drew.
        """
        if self._fade_in > 0 and age < self._fade_in:
            t = age / self._fade_in
            return t * t * (3 - 2 * t)  # smoothstep: no visible start edge
        remaining = self._ttl - age
        if remaining >= self._fade:
            return 1.0
        return max(0.0, remaining / self._fade)

    def _slide_offset(self, age: float) -> int:
        if self._fade_in <= 0 or age >= self._fade_in or not self._slide:
            return 0
        progress = age / self._fade_in
        return int(round(self._slide * (1.0 - progress)))

    def paintEvent(self, _event) -> None:  # noqa: N802
        now = time.monotonic()
        alive = self._alive(now)
        if not alive:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self._font)

        metrics = QFontMetrics(self._font)
        wrap_width = max(120, int(self.width() * self._cfg.max_width_ratio))
        gap = max(2, metrics.height() // 3)

        # One stack per region: chat subtitles sit under the chat box, kill-feed
        # subtitles under the kill feed.
        groups: dict[int, list[tuple[_Line, float]]] = {}
        for line, age in alive:
            groups.setdefault(line.region, []).append((line, age))

        for region in sorted(groups):
            entries = groups[region][-self._cfg.max_lines :]
            blocks: list[tuple[_Line, float, int, int]] = []
            for line, age in entries:
                rect = metrics.boundingRect(
                    QRect(0, 0, wrap_width, 10_000),
                    Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter,
                    self._render_text(line),
                )
                blocks.append((line, self._alpha(age), rect.width(), rect.height()))
            if not blocks:
                continue

            total_h = sum(h for _, _, _, h in blocks) + gap * (len(blocks) - 1)
            left, top, centered_x = self._anchor_box(total_h, region)
            y = top
            for line, alpha, width, height in blocks:
                x = left + (wrap_width - width) // 2 if centered_x else left
                drift = self._slide_offset(now - line.born)
                self._draw_block(painter, line, alpha, x, y + drift, width, height, wrap_width)
                y += height + gap

    def _render_text(self, line: _Line) -> str:
        if self._cfg.show_source_text and line.source_text:
            return f"{line.text}\n{line.source_text}"
        return line.text

    def _roi_pixels(self, region: int = 0) -> QRect | None:
        """Pixel rect of the ROI a subtitle came from."""
        if not self._rois_rel:
            return None
        index = region if 0 <= region < len(self._rois_rel) else 0
        rel = self._rois_rel[index]
        if rel is None:
            return None
        fx, fy, fw, fh = rel
        return QRect(
            int(fx * self.width()),
            int(fy * self.height()),
            int(fw * self.width()),
            int(fh * self.height()),
        )

    def _anchor_box(self, total_h: int, region: int = 0) -> tuple[int, int, bool]:
        """Return (left, top, horizontally_centred) for one region's stack."""
        anchor = self._cfg.anchor.lower()
        margin = self._cfg.margin_px
        wrap_width = int(self.width() * self._cfg.max_width_ratio)

        if anchor.startswith("roi"):
            roi = self._roi_pixels(region)
            if roi is not None:
                centred_left = roi.center().x() - wrap_width // 2
                if "below" in anchor:
                    top = roi.bottom() + self._cfg.roi_gap_px
                elif "above" in anchor:
                    top = roi.top() - self._cfg.roi_gap_px - total_h
                else:  # roi-over: cover the original text
                    top = roi.center().y() - total_h // 2
                # Never let the stack leave the screen.
                top = max(margin, min(top, self.height() - margin - total_h))
                left = max(margin, min(centred_left, self.width() - margin - wrap_width))
                return left, top, True
            # No ROI configured -> behave like a classic bottom-centred subtitle.
            return (
                (self.width() - wrap_width) // 2,
                max(margin, self.height() - margin - total_h),
                True,
            )

        if "bottom" in anchor:
            top = self.height() - margin - total_h
        elif "top" in anchor:
            top = margin
        else:
            top = (self.height() - total_h) // 2

        if "left" in anchor:
            return margin, top, False
        if "right" in anchor:
            return self.width() - margin - wrap_width, top, False
        return (self.width() - wrap_width) // 2, top, True

    def _draw_block(
        self,
        painter: QPainter,
        line: _Line,
        alpha: float,
        x: int,
        y: int,
        width: int,
        height: int,
        wrap_width: int,
    ) -> None:
        text = self._render_text(line)
        flags = Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter

        if self._cfg.background:
            panel = QColor(0, 0, 0, int(self._cfg.background_opacity * 255 * alpha))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(panel)
            pad_x, pad_y = 10, 5
            painter.drawRoundedRect(
                QRect(x - pad_x, y - pad_y, width + pad_x * 2, height + pad_y * 2), 6, 6
            )

        # Outline: draw the glyphs offset in black first, then the fill on top.
        # Far cheaper than QPainterPath stroking and reads well over any game.
        if self._cfg.outline:
            outline = QColor(0, 0, 0, int(230 * alpha))
            painter.setPen(QPen(outline))
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                painter.drawText(QRect(x + dx, y + dy, width, height), flags, text)

        fill = QColor(line.color)
        fill.setAlpha(int(line.color.alpha() * alpha))
        painter.setPen(QPen(fill))
        painter.drawText(QRect(x, y, width, height), flags, text)

        if self._cfg.show_latency and line.latency_ms > 0:
            small = QFont(self._font)
            small.setPixelSize(max(9, self._font.pixelSize() * 2 // 3))
            small.setWeight(QFont.Weight.Normal)
            painter.setFont(small)
            badge = QColor(150, 150, 150, int(170 * alpha))
            painter.setPen(QPen(badge))
            painter.drawText(
                QRect(x, y + height - 2, width, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                f"{line.latency_ms:.0f}ms",
            )
            painter.setFont(self._font)


class OverlayController(QObject):
    """Bridges the event bus (worker thread) to the widget (GUI thread)."""

    _deliver = Signal(object)

    def __init__(self, overlay: SubtitleOverlay, bus: EventBus) -> None:
        super().__init__()
        self._overlay = overlay
        self._unsubscribe = None
        # Queued: the pipeline publishes from its own thread, and Qt widgets may
        # only be touched from the GUI thread.
        self._deliver.connect(self._overlay.add_subtitle, Qt.ConnectionType.QueuedConnection)
        self._unsubscribe = bus.subscribe(Subtitle, self._on_subtitle)

    def _on_subtitle(self, subtitle: Subtitle) -> None:
        self._deliver.emit(subtitle)  # thread-safe: queued to the GUI thread

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)  # the overlay is a Tool window
    return app
