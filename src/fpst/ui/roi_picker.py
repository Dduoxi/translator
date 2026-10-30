"""Mouse-driven ROI picker supporting several regions.

Asking a user to hand-compute ``0.0 0.62 0.36 0.16`` is hostile, and a game
usually needs more than one region (chat in one corner, kill feed in another),
so this draws a dimmed full-screen layer where you drag as many boxes as you
want, then confirm.

Fractions (not pixels) are what we store, so the same config survives a
resolution change -- see :meth:`fpst.core.types.Rect.from_relative`.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

__all__ = ["PickedRegion", "RoiPicker", "pick_regions"]

ACCENT = QColor(0, 255, 170)
DRAFT = QColor(255, 210, 80)
DIM = QColor(0, 0, 0, 130)
HINT_BG = QColor(0, 0, 0, 200)

HINT_IDLE = "拖动鼠标框选区域（可框多个）    Enter 确认    Backspace 撤销    Esc 取消"
MIN_SIZE = 8  # px; anything smaller is a stray click


@dataclass(frozen=True)
class PickedRegion:
    monitor: int
    rect_px: tuple[int, int, int, int]
    fractions: tuple[float, float, float, float]
    monitor_size: tuple[int, int]

    @property
    def cli_region(self) -> str:
        return " ".join(f"{v:.3f}" for v in self.fractions)

    @property
    def toml_region(self) -> str:
        return "[" + ", ".join(f"{v:.3f}" for v in self.fractions) + "]"


def toml_regions(regions: list[PickedRegion]) -> str:
    """Render the whole list as one TOML array-of-arrays."""
    inner = ", ".join(r.toml_region for r in regions)
    return f"[{inner}]"


class RoiPicker(QWidget):
    def __init__(self, screen, monitor_index: int) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setWindowTitle("fpst roi picker")
        self.setGeometry(screen.geometry())

        self._screen = screen
        self._monitor_index = monitor_index
        self._origin = QPoint()
        self._draft = QRect()
        self._dragging = False
        self._boxes: list[QRect] = []
        self.result: list[PickedRegion] = []

    # ------------------------------------------------------------- rendering
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), DIM)

        for index, box in enumerate(self._boxes, start=1):
            self._draw_box(painter, box, index, DRAFT if False else ACCENT)

        draft = self._draft.normalized()
        if self._dragging and draft.width() > 2 and draft.height() > 2:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(DRAFT, 2, Qt.PenStyle.DashLine))
            painter.drawRect(draft.intersected(self.rect()))

        self._draw_hint(painter)
        if self._boxes:
            self._draw_summary(painter)

    def _draw_box(self, painter: QPainter, box: QRect, index: int, colour: QColor) -> None:
        sel = box.normalized().intersected(self.rect())
        if sel.isEmpty():
            return
        # Punch a fully transparent hole so the user sees the real screen.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(sel, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        painter.setPen(QPen(colour, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(sel)

        # Index badge, so the user can tell the regions apart in the summary.
        painter.setFont(QFont("Monospace", 10, QFont.Weight.Bold))
        badge = QRect(sel.left() + 3, sel.top() + 3, 22, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 200))
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(colour)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(index))

    def _rows(self) -> list[str]:
        width, height = max(1, self.width()), max(1, self.height())
        rows = []
        for index, box in enumerate(self._boxes, start=1):
            sel = box.normalized()
            rows.append(
                f"{index}. {sel.width():4}x{sel.height():<4} "
                f"{sel.x() / width:.3f} {sel.y() / height:.3f} "
                f"{sel.width() / width:.3f} {sel.height() / height:.3f}"
            )
        return rows

    def _draw_summary(self, painter: QPainter) -> None:
        rows = self._rows()
        if not rows:
            return
        painter.setFont(QFont("Monospace", 10, QFont.Weight.Bold))
        metrics = painter.fontMetrics()
        box_w = max(metrics.horizontalAdvance(r) for r in rows) + 24
        box_h = metrics.height() * len(rows) + 20
        x = self.width() - box_w - 16
        y = 16
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(HINT_BG)
        painter.drawRoundedRect(QRect(int(x), int(y), int(box_w), int(box_h)), 8, 8)
        painter.setPen(QColor(200, 255, 230))
        for i, row in enumerate(rows):
            painter.drawText(
                int(x + 12),
                int(y + 12 + metrics.ascent() + i * metrics.height()),
                row,
            )

    def _draw_hint(self, painter: QPainter) -> None:
        text = HINT_IDLE if not self._boxes else f"{len(self._boxes)} 个区域  ·  " + HINT_IDLE
        painter.setFont(QFont("Sans Serif", 13, QFont.Weight.Bold))
        metrics = painter.fontMetrics()
        box_w = metrics.horizontalAdvance(text) + 40
        box_h = metrics.height() + 26
        box = QRect(
            int((self.width() - box_w) / 2),
            int(self.height() * 0.46),
            int(box_w),
            int(box_h),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(HINT_BG)
        painter.drawRoundedRect(box, 10, 10)
        painter.setPen(QColor(235, 245, 255))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # A fullscreen tool window does not reliably get keyboard focus from the
        # WM, and without it Enter/Esc do nothing. Grab it explicitly, and retry
        # once the window is actually mapped.
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.grabKeyboard()
        QTimer.singleShot(0, self._ensure_focus)

    def _ensure_focus(self) -> None:
        if self.isVisible() and not self.hasFocus():
            self.activateWindow()
            self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def hideEvent(self, event) -> None:  # noqa: N802
        self.releaseKeyboard()
        super().hideEvent(event)

    def _ratio(self) -> float:
        try:
            return float(self._screen.devicePixelRatio())
        except Exception:  # noqa: BLE001
            return 1.0

    # ---------------------------------------------------------------- input
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._draft = QRect(self._origin, self._origin)
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._draft = QRect(self._origin, event.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._draft = QRect(self._origin, event.position().toPoint())
            self._commit_draft()
            self.update()

    def _commit_draft(self) -> None:
        sel = self._draft.normalized().intersected(self.rect())
        self._draft = QRect()
        if sel.width() < MIN_SIZE or sel.height() < MIN_SIZE:
            return  # stray click
        self._boxes.append(sel)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.result = []
            self._finish()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._dragging:
                self._dragging = False
                self._commit_draft()
            if self._boxes:
                self.result = [self._make_result(box) for box in self._boxes]
                self._finish()
        elif key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            if self._dragging:
                self._dragging = False
                self._draft = QRect()
            elif self._boxes:
                self._boxes.pop()
            self.update()
        elif key == Qt.Key.Key_R:
            self._boxes.clear()
            self._draft = QRect()
            self._dragging = False
            self.result = []
            self.update()

    def _finish(self) -> None:
        """Close and exit the event loop.

        A ``Qt.Tool`` window does not count as "the last window" for
        ``quitOnLastWindowClosed``, so closing alone would leave
        ``QApplication.exec()`` spinning forever and the caller hanging.
        """
        self.releaseKeyboard()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ----------------------------------------------------------- conversions
    def _fractions(self, sel: QRect) -> tuple[float, float, float, float]:
        """Express ``sel`` (widget coords) as fractions of the *monitor*.

        Not of the widget: a window manager may shrink or offset our fullscreen
        window by a few pixels, and storing widget-relative fractions would then
        drift every time the config is used.
        """
        geo = self._screen.geometry()
        mon_w = max(1, geo.width())
        mon_h = max(1, geo.height())
        off_x = self.x() - geo.x()
        off_y = self.y() - geo.y()
        return (
            (sel.x() + off_x) / mon_w,
            (sel.y() + off_y) / mon_h,
            sel.width() / mon_w,
            sel.height() / mon_h,
        )

    def _make_result(self, sel: QRect) -> PickedRegion:
        ratio = self._ratio()
        geo = self._screen.geometry()
        fx, fy, fw, fh = self._fractions(sel)
        # Qt works in logical pixels; capture works in physical ones.
        mon_px_w, mon_px_h = round(geo.width() * ratio), round(geo.height() * ratio)
        return PickedRegion(
            monitor=self._monitor_index,
            rect_px=(
                round(fx * mon_px_w),
                round(fy * mon_px_h),
                round(fw * mon_px_w),
                round(fh * mon_px_h),
            ),
            fractions=tuple(round(v, 4) for v in (fx, fy, fw, fh)),  # type: ignore[arg-type]
            monitor_size=(mon_px_w, mon_px_h),
        )


def pick_regions(monitor: int = 1) -> list[PickedRegion] | None:
    """Show the picker on ``monitor`` (1-based). ``None`` means cancelled."""
    app = QApplication.instance() or QApplication([])
    screens = app.screens()
    if not screens:
        raise RuntimeError("no display available")
    index = max(0, min(monitor - 1, len(screens) - 1))

    picker = RoiPicker(screens[index], index + 1)  # type: ignore[arg-type]
    picker.show()
    picker.raise_()
    picker.activateWindow()
    app.exec()
    return picker.result or None


# Kept for callers that only want one region.
def pick_region(monitor: int = 1) -> PickedRegion | None:
    regions = pick_regions(monitor)
    return regions[0] if regions else None
