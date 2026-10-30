from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fpst.ui.roi_picker import MIN_SIZE, PickedRegion, RoiPicker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def geometry(app):
    """The picker always covers exactly one monitor; tests should too."""
    return app.screens()[0].geometry()


@pytest.fixture
def picker(app, geometry):
    widget = RoiPicker(app.screens()[0], 1)
    widget.setGeometry(geometry)
    yield widget
    widget.close()


def _at(geometry: QRect, fx: float, fy: float, fw: float, fh: float) -> QRect:
    """Build a widget-space QRect from monitor fractions."""
    return QRect(
        int(fx * geometry.width()),
        int(fy * geometry.height()),
        int(fw * geometry.width()),
        int(fh * geometry.height()),
    )


# ------------------------------------------------------------------ formatting


def test_picked_region_formatting():
    picked = PickedRegion(1, (10, 20, 30, 40), (0.1, 0.2, 0.3, 0.4), (1920, 1080))
    assert picked.cli_region == "0.100 0.200 0.300 0.400"
    assert picked.toml_region == "[0.100, 0.200, 0.300, 0.400]"


# ------------------------------------------------------------------ geometry


def test_fractions_are_relative_to_the_monitor(picker, geometry):
    result = picker._make_result(_at(geometry, 0.1, 0.25, 0.5, 0.3))
    assert result.fractions == pytest.approx((0.1, 0.25, 0.5, 0.3), abs=1e-3)
    assert result.monitor_size == (geometry.width(), geometry.height())


def test_fractions_roundtrip_to_the_same_pixels(picker, geometry):
    """Stored fractions must reproduce the box -- that is the whole point."""
    from fpst.core.types import Rect

    box = _at(geometry, 0.12, 0.6, 0.4, 0.2)
    result = picker._make_result(box)
    rect = Rect.from_relative(*result.fractions, geometry.width(), geometry.height())
    assert rect.xywh == pytest.approx(box.getRect(), abs=2)


def test_window_offset_is_compensated(app, geometry):
    """A WM that shifts our window must not skew the fractions."""
    widget = RoiPicker(app.screens()[0], 1)
    shifted = geometry.translated(37, 21)
    widget.setGeometry(shifted)
    try:
        # Same on-screen box, but expressed in the shifted widget's coordinates
        sel = _at(geometry, 0.2, 0.4, 0.3, 0.2).translated(-37, -21)
        result = widget._make_result(sel)
        assert result.fractions == pytest.approx((0.2, 0.4, 0.3, 0.2), abs=1e-3)
    finally:
        widget.close()


def test_empty_selection_is_rejected(picker):
    picker._sel = QRect(10, 10, MIN_SIZE - 1, 100)
    assert picker._selection().isEmpty()


def test_selection_is_normalised_when_dragged_up_left(picker):
    picker._sel = QRect(300, 300, -200, -200)
    sel = picker._selection()
    assert sel.width() == 200 and sel.height() == 200


def test_selection_clipped_to_widget(picker, geometry):
    picker._sel = QRect(geometry.width() - 50, geometry.height() - 50, 500, 500)
    sel = picker._selection()
    assert sel.right() <= geometry.width()
    assert sel.bottom() <= geometry.height()


# --------------------------------------------------------------------- input


def test_full_drag_creates_a_result(app, geometry):
    from PySide6.QtTest import QTest

    widget = RoiPicker(app.screens()[0], 1)
    widget.setGeometry(geometry)
    widget.show()
    app.processEvents()

    start = QPoint(int(geometry.width() * 0.2), int(geometry.height() * 0.2))
    end = QPoint(int(geometry.width() * 0.6), int(geometry.height() * 0.6))
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end)
    app.processEvents()

    assert widget.result is not None
    assert widget.result.fractions == pytest.approx((0.2, 0.2, 0.4, 0.4), abs=2e-3)
    widget.close()


def test_escape_clears_result(picker):
    picker._sel = QRect(10, 10, 100, 100)
    picker.result = PickedRegion(1, (0, 0, 1, 1), (0, 0, 1, 1), (10, 10))
    event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    picker.keyPressEvent(event)
    assert picker.result is None


def test_enter_confirms_selection(picker, geometry):
    picker._sel = _at(geometry, 0.1, 0.2, 0.4, 0.6)
    event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    picker.keyPressEvent(event)
    assert picker.result is not None
    assert picker.result.fractions == pytest.approx((0.1, 0.2, 0.4, 0.6), abs=1e-3)


def test_r_resets_the_selection(picker):
    picker._sel = QRect(10, 10, 100, 100)
    picker.result = PickedRegion(1, (0, 0, 1, 1), (0, 0, 1, 1), (10, 10))
    event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier)
    picker.keyPressEvent(event)
    assert picker.result is None
    assert picker._selection().isEmpty()


def test_finish_quits_the_event_loop(app, picker):
    """Without an explicit quit(), Qt.Tool windows leave exec() spinning."""
    picker._sel = QRect(10, 10, 100, 100)
    picker._finish()
    assert not picker.isVisible()


def test_widget_renders_without_error(picker, geometry):
    picker._sel = QRect()
    picker.grab()
    picker._sel = _at(geometry, 0.05, 0.6, 0.35, 0.25)
    picker.grab()
