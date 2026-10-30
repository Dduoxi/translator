"""Application controller: owns the config, the pipeline and the overlay.

Everything the tray and the settings window can do goes through here, so the UI
stays a thin shell and the behaviour is testable without a mouse.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..core.bus import EventBus
from ..core.config import AppConfig, default_config_path
from ..core.types import Subtitle
from ..pipeline import ScreenPipeline

log = logging.getLogger(__name__)

__all__ = ["AppController", "RunState"]

IDLE_RESET_S = 20.0
"""Seconds without a subtitle before the status reads 'idle' rather than 'live'."""


class RunState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class AppController(QObject):
    """Start/stop the pipeline, reload config, expose live status."""

    state_changed = Signal(str)  # RunState
    subtitle_seen = Signal()
    error = Signal(str)

    def __init__(self, config_path: str | Path | None = None) -> None:
        super().__init__()
        self._config_path = Path(config_path) if config_path else default_config_path()
        self.cfg = AppConfig.load(self._config_path)
        self.bus = EventBus()
        self._pipeline: ScreenPipeline | None = None
        self._overlay = None  # set by the app; SubtitleOverlay
        self._state = RunState.STOPPED
        self._last_subtitle_at = 0.0
        self._last_error = ""
        self.subtitle_count = 0
        self.bus.subscribe(Subtitle, self._on_subtitle)

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> str:
        return self._state

    @property
    def config_path(self) -> Path:
        return self._config_path

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def attach_overlay(self, overlay) -> None:
        self._overlay = overlay

    def _on_subtitle(self, _subtitle: Subtitle) -> None:
        self.subtitle_count += 1
        self._last_subtitle_at = time.monotonic()
        self.subtitle_seen.emit()

    # -------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self._pipeline is not None:
            return True
        try:
            pipeline = ScreenPipeline.from_config(self.cfg, self.bus)
            pipeline.start()
        except Exception as exc:  # noqa: BLE001 - surface it in the UI, don't die
            self._last_error = str(exc)
            log.exception("pipeline failed to start")
            self.error.emit(self._last_error)
            self._set_state(RunState.ERROR)
            return False
        self._pipeline = pipeline
        self._set_state(RunState.RUNNING)
        return True

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
        self._set_state(RunState.STOPPED)

    def pause(self) -> None:
        if self._pipeline is not None:
            self.stop()
            self._set_state(RunState.PAUSED)

    def resume(self) -> bool:
        return self.start()

    def toggle(self) -> None:
        if self._pipeline is None:
            self.start()
        else:
            self.pause()

    def shutdown(self) -> None:
        self.stop()

    # ----------------------------------------------------------------- config
    def reload_config(self) -> None:
        """Re-read config.toml (the ROI picker and wizard write it directly)."""
        was_running = self._pipeline is not None
        self.stop()
        self.cfg = AppConfig.load(self._config_path)
        if self._overlay is not None:
            self._overlay.apply_config(self.cfg.ui, rois=self.cfg.screen.resolved_regions())
        if was_running:
            self.start()

    def save_config(self, cfg: AppConfig | None = None) -> Path:
        if cfg is not None:
            self.cfg = cfg
        return self.cfg.save(self._config_path)

    def apply_and_restart(self, cfg: AppConfig) -> Path:
        """Persist edited settings and restart so the change takes effect."""
        was_running = self._pipeline is not None
        self.stop()
        self.cfg = cfg
        path = self.save_config()
        if self._overlay is not None:
            self._overlay.apply_config(cfg.ui, rois=cfg.screen.resolved_regions())
        if was_running:
            self.start()
        return path

    # ------------------------------------------------------------------ stats
    def snapshot(self) -> dict[str, object]:
        """Live numbers for the status panel (safe to call at any time)."""
        idle_for = time.monotonic() - self._last_subtitle_at if self._last_subtitle_at else None
        data: dict[str, object] = {
            "state": self._state,
            "subtitles": self.subtitle_count,
            "idle_for": idle_for,
            "last_error": self._last_error,
            "regions": len(self.cfg.screen.resolved_regions()),
            "model": self.cfg.translate.model,
        }
        if self._pipeline is not None:
            stats = self._pipeline.stats
            data.update(
                {
                    "frames": stats.frames_grabbed,
                    "ocr_runs": stats.ocr_runs,
                    "ocr_ms": stats.ocr_ms,
                    "translate_ms": stats.translate_ms,
                    "lines_new": stats.lines_new,
                    "errors": stats.errors,
                }
            )
            translator = getattr(self._pipeline, "translator_stats", None)
            if translator:
                data["cache"] = translator
        return data

    def status_text(self) -> str:
        if self._state == RunState.ERROR:
            return f"错误：{self._last_error}"
        if self._state == RunState.STOPPED:
            return "已停止"
        if self._state == RunState.PAUSED:
            return "已暂停"
        idle = time.monotonic() - self._last_subtitle_at if self._last_subtitle_at else None
        if idle is None or idle > IDLE_RESET_S:
            return "运行中 · 等待画面变化"
        return f"运行中 · 最近一条 {idle:.0f}s 前"


class StatusPoller:
    """Calls ``on_tick`` on a timer; split out so tests can drive it manually."""

    def __init__(self, controller: AppController, on_tick, interval_ms: int = 1000) -> None:
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(lambda: on_tick(controller.snapshot()))
        self._controller = controller

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
