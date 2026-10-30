"""Pick a QScreen by position instead of by index.

Why this exists: the capture side identifies a monitor by its absolute origin
(mss and Qt order their screens differently -- mss inserts an all-displays union
at index 0). If the overlay and the ROI picker use a raw Qt index while capture
uses an origin, they can end up on different physical screens: the subtitles
appear on one display while the OCR watches another.

Everything therefore takes an origin and falls back to an index only when the
origin is unknown.
"""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication, QScreen

__all__ = ["find_screen", "screen_origin"]


def find_screen(origin: tuple[int, int] | None = None, index: int = 0) -> QScreen:
    """Return the screen at ``origin``, else the one at ``index`` (0-based)."""
    screens = QGuiApplication.screens()
    if not screens:
        raise RuntimeError("no display available")
    if origin is not None:
        for screen in screens:
            geometry = screen.geometry()
            if (geometry.x(), geometry.y()) == (origin[0], origin[1]):
                return screen
    return screens[max(0, min(index, len(screens) - 1))]


def screen_origin(screen: QScreen) -> tuple[int, int]:
    geometry = screen.geometry()
    return (geometry.x(), geometry.y())
