"""End-to-end pipelines that wire one or more capture sources to the event bus."""

from __future__ import annotations

from .screen import PipelineStats, ScreenPipeline

__all__ = ["ScreenPipeline", "PipelineStats"]
