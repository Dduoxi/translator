"""Import every built-in engine so its @register runs.

Optional heavy dependencies (onnxruntime, ctranslate2, PySide6, ...) are allowed
to be missing -- we just skip that engine. This is what makes the same checkout
usable on the Linux dev box (light) and the Windows gaming PC (full).
"""

from __future__ import annotations

import importlib
import logging

__all__ = ["load_builtin_engines", "MISSING"]

log = logging.getLogger(__name__)

MODULES = [
    "translator.translate.mock",
    "translator.ocr.mock",
    "translator.capture.mss_source",
    "translator.capture.dxgi",
    "translator.ocr.rapidocr_engine",
    "translator.ocr.windows_engine",
    "translator.translate.baidu",
    "translator.translate.llm",
]

MISSING: dict[str, str] = {}


def load_builtin_engines() -> dict[str, str]:
    """Import all engine modules; return {module: reason} for the ones skipped."""
    MISSING.clear()
    for name in MODULES:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            MISSING[name] = str(exc)
            log.debug("engine module %s unavailable: %s", name, exc)
    return dict(MISSING)
