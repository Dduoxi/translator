"""RapidOCR (PP-OCRv4 mobile) implementation of :class:`OCREngine`.

Tuning notes, measured on a 16-thread Ryzen 4800U, 760x240 crop with 4 chat
lines. The lesson is that **wall-clock and CPU cost pull in opposite
directions**, and for a background tool CPU is what matters:

    threads   wall     CPU     CPU at 1 fps
    1         688 ms   781 ms   78% of a core   <- chosen
    2         502 ms  1168 ms  117%
    4         333 ms  1655 ms  166%
    8         371 ms  3325 ms  333%   <- onnxruntime's default did this

8 threads burn 4.3x the CPU of 1 thread to save 1.9x the latency. We are a
overlay running next to a game that needs the cores; we must be the cheapest
thing on the machine, not the fastest. So: one thread, and instead buy latency
back by throttling *when* we run rather than *how fast* we run.

Other findings:

* ``rec_batch_num`` must be 1. Batching pads every line to the longest line's
  width, so one 44-character message makes the short lines 4x more expensive.
* ``rec_img_shape`` height 48 -> 32 is the single biggest win: identical text
  on 4/4 chat lines at 2.3x less CPU (922 -> 399 ms). The recogniser input is
  3xHxW with W proportional to H, so cost is quadratic in height. Height 24
  breaks recognition entirely, so 32 is the floor for chat-sized glyphs.
* ``Det.limit_type`` must be "max". The bundled default ("min") *upscales* the
  crop to 736px on the short side -- 4x the pixels for an already-legible crop.
* The recogniser dominates (75 ms det vs 289 ms rec). The lever is therefore not
running OCR at all when nothing changed -- see
:class:`translator.capture.gate.ChangedOnly`.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

from ..core.interfaces import OCREngine
from ..core.registry import register
from ..core.types import Frame, Lang, Source, TextRegion

log = logging.getLogger(__name__)

__all__ = ["RapidOCREngine", "default_threads"]


def default_threads() -> int:
    """One thread on purpose -- see the module docstring.

    Override only if you accept multi-core burn: ``[ocr] threads = 2`` roughly
    halves wall-clock latency at 1.5x the CPU cost.
    """
    return 1


class RapidOCREngine(OCREngine):
    name = "rapidocr"

    def __init__(
        self,
        lang: str = "en",
        min_confidence: float = 0.55,
        threads: int = 0,
        det_limit_side: int = 960,
        use_cls: bool = False,
        max_det_side: int = 2000,
        rec_batch: int = 1,
        rec_height: int = 32,
        **_ignored: object,
    ) -> None:
        self._lang = lang
        self._min_conf = min_confidence
        self._threads = threads or default_threads()
        self._det_limit_side = det_limit_side
        self._use_cls = use_cls
        self._max_det_side = max_det_side
        self._rec_batch = max(1, int(rec_batch))
        # Height of the recogniser's normalised input. The bundled default is
        # 48; 32 measures identically on chat-sized text at 2.3x less CPU,
        # because the input is 3xHxW and W scales with H -- so cost is
        # quadratic in this number. 24 breaks recognition outright.
        self._rec_height = max(16, int(rec_height))
        self._engine = None
        self._config_file: str | None = None

    # ------------------------------------------------------------- lifecycle
    def _build_config(self) -> str:
        """Patch RapidOCR's bundled yaml and return the temp file path.

        RapidOCR only accepts thread counts and det limits through the config
        file, and its recursive YAML anchors mean ``**kwargs`` can't reach the
        Det/Rec sections.
        """
        import rapidocr_onnxruntime as pkg
        import yaml

        base_path = Path(pkg.__file__).parent / "config.yaml"
        with base_path.open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)

        for section in ("Global", "Det", "Cls", "Rec"):
            if isinstance(cfg.get(section), dict):
                cfg[section]["intra_op_num_threads"] = self._threads
                cfg[section]["inter_op_num_threads"] = 1
        cfg["Global"]["use_cls"] = self._use_cls
        cfg["Global"]["print_verbose"] = False
        cfg["Global"]["max_side_len"] = self._max_det_side
        cfg["Det"]["limit_side_len"] = self._det_limit_side
        # "max" = downscale only. The bundled default ("min") *upscales* small
        # crops to 736px on the short side, which is 4x the pixels for a crop
        # that is already legible.
        cfg["Det"]["limit_type"] = "max"
        if "rec_batch_num" in cfg.get("Rec", {}):
            cfg["Rec"]["rec_batch_num"] = self._rec_batch
        if "rec_img_shape" in cfg.get("Rec", {}):
            shape = list(cfg["Rec"]["rec_img_shape"])
            shape[1] = self._rec_height
            cfg["Rec"]["rec_img_shape"] = shape

        fd, path = tempfile.mkstemp(suffix=".yaml", prefix="translator-ocr-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, allow_unicode=True)
        return path

    def warmup(self) -> None:
        if self._engine is not None:
            return
        from rapidocr_onnxruntime import RapidOCR

        self._config_file = self._build_config()
        with redirect_stdout(io.StringIO()):
            self._engine = RapidOCR(config_path=self._config_file)
            # First inference loads the ONNX sessions (~3s); do it now so the
            # first real frame isn't 3s late.
            self._engine(np.zeros((48, 192, 3), dtype=np.uint8))
        log.info("RapidOCR ready: threads=%d det_side=%d", self._threads, self._det_limit_side)

    def close(self) -> None:
        self._engine = None
        if self._config_file:
            with contextlib.suppress(OSError):
                os.unlink(self._config_file)
            self._config_file = None

    # -------------------------------------------------------------- recognise
    def recognize(self, frame: Frame) -> list[TextRegion]:
        if self._engine is None:
            self.warmup()
        assert self._engine is not None
        with redirect_stdout(io.StringIO()):
            result, _elapse = self._engine(frame.image)

        regions: list[TextRegion] = []
        for box, text, score in result or ():
            if float(score) < self._min_conf:
                continue
            cleaned = str(text).strip()
            if not cleaned:
                continue
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            regions.append(
                TextRegion(
                    text=cleaned,
                    source=Source.SCREEN,
                    lang=Lang(self._lang) if self._lang in Lang._value2member_map_ else Lang.AUTO,
                    confidence=float(score),
                    bbox=(int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))),
                )
            )
        regions.sort(key=lambda r: (r.bbox[1] // 8, r.bbox[0]) if r.bbox else (0, 0))
        return regions


@register(OCREngine, "rapidocr")
def _factory(**kwargs: object) -> OCREngine:
    return RapidOCREngine(**kwargs)  # type: ignore[arg-type]
