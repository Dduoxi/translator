from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fpst.ui.roi_picker import (  # noqa: E402
    MIN_SIZE,
    PickedRegion,
    RoiPicker,
    toml_regions,
)


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
    return QRect(
        int(fx * geometry.width()),
        int(fy * geometry.height()),
        int(fw * geometry.width()),
        int(fh * geometry.height()),
    )


def _key(widget: RoiPicker, key) -> None:
    widget.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


# ------------------------------------------------------------------ formatting


def test_picked_region_formatting():
    picked = PickedRegion(1, (10, 20, 30, 40), (0.1, 0.2, 0.3, 0.4), (1920, 1080))
    assert picked.cli_region == "0.100 0.200 0.300 0.400"
    assert picked.toml_region == "[0.100, 0.200, 0.300, 0.400]"


def test_toml_regions_renders_nested_arrays():
    a = PickedRegion(1, (0, 0, 1, 1), (0.1, 0.2, 0.3, 0.4), (1920, 1080))
    b = PickedRegion(1, (0, 0, 1, 1), (0.5, 0.6, 0.2, 0.1), (1920, 1080))
    assert toml_regions([a, b]) == "[[0.100, 0.200, 0.300, 0.400], [0.500, 0.600, 0.200, 0.100]]"


# -------------------------------------------------------------------- geometry


def test_fractions_are_relative_to_the_monitor(picker, geometry):
    result = picker._make_result(_at(geometry, 0.1, 0.25, 0.5, 0.3))
    assert result.fractions == pytest.approx((0.1, 0.25, 0.5, 0.3), abs=1e-3)
    assert result.monitor_size == (geometry.width(), geometry.height())


def test_fractions_roundtrip_to_the_same_pixels(picker, geometry):
    from fpst.core.types import Rect

    box = _at(geometry, 0.12, 0.6, 0.4, 0.2)
    result = picker._make_result(box)
    rect = Rect.from_relative(*result.fractions, geometry.width(), geometry.height())
    assert rect.xywh == pytest.approx(box.getRect(), abs=2)


def test_window_offset_is_compensated(app, geometry):
    widget = RoiPicker(app.screens()[0], 1)
    widget.setGeometry(geometry.translated(37, 21))
    try:
        sel = _at(geometry, 0.2, 0.4, 0.3, 0.2).translated(-37, -21)
        assert widget._make_result(sel).fractions == pytest.approx((0.2, 0.4, 0.3, 0.2), abs=1e-3)
    finally:
        widget.close()


# ----------------------------------------------------------------- multi-select


def test_drag_appends_a_box(picker, geometry):
    picker._draft = _at(geometry, 0.1, 0.1, 0.2, 0.2)
    picker._commit_draft()
    picker._draft = _at(geometry, 0.6, 0.6, 0.2, 0.2)
    picker._commit_draft()
    assert len(picker._boxes) == 2


def test_tiny_drag_is_rejected(picker):
    picker._draft = QRect(10, 10, MIN_SIZE - 1, MIN_SIZE - 1)
    picker._commit_draft()
    assert picker._boxes == []


def test_drag_up_left_is_normalised(picker):
    picker._draft = QRect(300, 300, -200, -200)
    picker._commit_draft()
    assert picker._boxes[0].width() == 200
    assert picker._boxes[0].height() == 200


def test_box_is_clipped_to_the_widget(picker, geometry):
    picker._draft = QRect(geometry.width() - 50, geometry.height() - 50, 500, 500)
    picker._commit_draft()
    assert picker._boxes[0].right() <= geometry.width()


def test_enter_confirms_all_boxes(picker, geometry):
    picker._boxes = [
        _at(geometry, 0.0, 0.6, 0.3, 0.2),
        _at(geometry, 0.7, 0.0, 0.3, 0.2),
    ]
    _key(picker, Qt.Key.Key_Return)
    assert len(picker.result) == 2
    assert picker.result[0].fractions == pytest.approx((0.0, 0.6, 0.3, 0.2), abs=1e-3)
    assert picker.result[1].fractions == pytest.approx((0.7, 0.0, 0.3, 0.2), abs=1e-3)


def test_enter_with_no_boxes_does_nothing(picker):
    _key(picker, Qt.Key.Key_Return)
    assert picker.result == []


def test_backspace_removes_the_last_box(picker, geometry):
    picker._boxes = [_at(geometry, 0.0, 0.0, 0.2, 0.2), _at(geometry, 0.5, 0.5, 0.2, 0.2)]
    _key(picker, Qt.Key.Key_Backspace)
    assert len(picker._boxes) == 1
    assert picker._boxes[0].top() < 10, "the first box must survive"


def test_r_clears_everything(picker, geometry):
    picker._boxes = [_at(geometry, 0.0, 0.0, 0.2, 0.2)]
    _key(picker, Qt.Key.Key_R)
    assert picker._boxes == []
    assert picker.result == []


def test_escape_cancels(picker, geometry):
    picker._boxes = [_at(geometry, 0.0, 0.0, 0.2, 0.2)]
    _key(picker, Qt.Key.Key_Escape)
    assert picker.result == []


def test_enter_commits_an_in_progress_drag(picker, geometry):
    """Pressing Enter mid-drag should still register what was drawn."""
    picker._dragging = True
    picker._draft = _at(geometry, 0.2, 0.2, 0.3, 0.3)
    _key(picker, Qt.Key.Key_Return)
    assert len(picker.result) == 1


# --------------------------------------------------------------------- render


def test_summary_rows_describe_every_box(picker, geometry):
    picker._boxes = [
        _at(geometry, 0.0, 0.6, 0.3, 0.2),
        _at(geometry, 0.7, 0.0, 0.3, 0.2),
    ]
    rows = picker._rows()
    assert len(rows) == 2
    assert rows[0].startswith("1.")
    assert rows[1].startswith("2.")


def test_widget_renders_in_every_state(picker, geometry):
    picker._boxes = []
    picker.grab()
    picker._boxes = [_at(geometry, 0.05, 0.6, 0.35, 0.25)]
    picker.grab()
    picker._dragging = True
    picker._draft = _at(geometry, 0.5, 0.1, 0.3, 0.2)
    picker.grab()
