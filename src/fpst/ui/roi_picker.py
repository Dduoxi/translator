"""Mouse-driven ROI picker.

Asking a user to hand-compute ``0.0 0.62 0.36 0.16`` is hostile. This draws a
dimmed full-screen layer over the target monitor, lets them drag a box, and
hands back the fractional coordinates ready to paste into ``config.toml``.

Fractions (not pixels) are what we store, so the same config survives a
resolution change -- see :meth:`fpst.core.types.Rect.from_relative`.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

__all__ = ["PickedRegion", "RoiPicker", "pick_region"]

ACCENT = QColor(0, 255, 170)
DIM = QColor(0, 0, 0, 130)
HINT_BG = QColor(0, 0, 0, 190)

HINT = "拖动鼠标框选要识别的区域    Enter 确认    Esc 取消    R 重选"
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
        self._sel = QRect()
        self._dragging = False
        self.result: PickedRegion | None = None

    # ------------------------------------------------------------- rendering
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), DIM)

        sel = self._selection()
        if sel.isEmpty():
            self._draw_hint(painter)
            return

        # Punch a fully transparent hole so the user sees the real screen.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(sel, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        painter.setPen(QPen(ACCENT, 2))
        painter.drawRect(sel)
        self._draw_corners(painter, sel)
        self._draw_readout(painter, sel)

    def _draw_corners(self, painter: QPainter, sel: QRect) -> None:
        painter.setPen(QPen(ACCENT, 4))
        arm = min(18, sel.width() // 3, sel.height() // 3)
        for x, y, dx, dy in (
            (sel.left(), sel.top(), 1, 1),
            (sel.right(), sel.top(), -1, 1),
            (sel.left(), sel.bottom(), 1, -1),
            (sel.right(), sel.bottom(), -1, -1),
        ):
            painter.drawLine(x, y, x + arm * dx, y)
            painter.drawLine(x, y, x, y + arm * dy)

    def _draw_readout(self, painter: QPainter, sel: QRect) -> None:
        fx, fy, fw, fh = self._fractions(sel)
        size = self._ratio()

        lines = [
            f"{round(sel.width() * size)} x {round(sel.height() * size)} px",
            f"region = {fx:.3f} {fy:.3f} {fw:.3f} {fh:.3f}",
        ]
        painter.setFont(QFont("Monospace", 11, QFont.Weight.Bold))
        metrics = painter.fontMetrics()
        box_w = max(metrics.horizontalAdvance(t) for t in lines) + 20
        box_h = metrics.height() * len(lines) + 14

        x = min(max(8, sel.left()), self.width() - box_w - 8)
        y = sel.top() - box_h - 8
        if y < 8:
            y = min(sel.bottom() + 8, self.height() - box_h - 8)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(HINT_BG)
        painter.drawRoundedRect(QRect(int(x), int(y), int(box_w), int(box_h)), 6, 6)
        painter.setPen(QColor(235, 245, 255))
        for i, text in enumerate(lines):
            painter.drawText(
                int(x + 10),
                int(y + 7 + metrics.ascent() + i * metrics.height()),
                text,
            )

    def _draw_hint(self, painter: QPainter) -> None:
        painter.setFont(QFont("Sans Serif", 14, QFont.Weight.Bold))
        metrics = painter.fontMetrics()
        box_w = metrics.horizontalAdvance(HINT) + 40
        box_h = metrics.height() + 28
        box = QRect(
            int((self.width() - box_w) / 2),
            int(self.height() * 0.42),
            int(box_w),
            int(box_h),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(HINT_BG)
        painter.drawRoundedRect(box, 10, 10)
        painter.setPen(QColor(235, 245, 255))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, HINT)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # A fullscreen tool window does not reliably get keyboard focus from the
        # WM, and without it Enter/Esc/R silently do nothing. Grab it explicitly,
        # and retry once the window is actually mapped.
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
    def _selection(self) -> QRect:
        sel = self._sel.normalized().intersected(self.rect())
        if sel.width() < MIN_SIZE or sel.height() < MIN_SIZE:
            return QRect()
        return sel

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._sel = QRect(self._origin, self._origin)
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._sel = QRect(self._origin, event.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._sel = QRect(self._origin, event.position().toPoint())
            sel = self._selection()
            if sel.isEmpty():
                self._sel = QRect()
            self.result = self._make_result(sel) if not sel.isEmpty() else None
            self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.result = None
            self._finish()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            sel = self._selection()
            if not sel.isEmpty():
                self.result = self._make_result(sel)
                self._finish()
        elif key == Qt.Key.Key_R:
            self._sel = QRect()
            self.result = None
            self.update()

    def _finish(self) -> None:
        """Close and exit the event loop.

        A ``Qt.Tool`` window does not count as "the last window" for
        ``quitOnLastWindowClosed``, so closing alone would leave
        ``QApplication.exec()`` spinning forever and ``pick_region`` hanging.
        """
        self.releaseKeyboard()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

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
        rect_px = (
            round(fx * mon_px_w),
            round(fy * mon_px_h),
            round(fw * mon_px_w),
            round(fh * mon_px_h),
        )
        return PickedRegion(
            monitor=self._monitor_index,
            rect_px=rect_px,
            fractions=tuple(round(v, 4) for v in (fx, fy, fw, fh)),  # type: ignore[arg-type]
            monitor_size=(mon_px_w, mon_px_h),
        )


def pick_region(monitor: int = 1) -> PickedRegion | None:
    """Show the picker on ``monitor`` (1-based) and return the selection."""
    app = QApplication.instance() or QApplication([])
    screens = app.screens()
    if not screens:
        raise RuntimeError("no display available")
    index = max(0, min(monitor - 1, len(screens) - 1))
    screen = screens[index]

    picker = RoiPicker(screen, index + 1)  # type: ignore[arg-type]
    picker.show()
    picker.raise_()
    picker.activateWindow()
    app.exec()
    return picker.result
