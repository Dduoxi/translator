"""Settings window: five tabs over the config tree, with a live appearance preview.

Binding strategy: each tab owns a ``load(cfg)`` and ``collect(cfg)`` pair. The
window edits a *copy* of the config, so Cancel is free and a half-edited form can
never corrupt a running pipeline.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.config import AppConfig
from ..core.types import Lang, Source, Subtitle

__all__ = ["SettingsWindow"]

ANCHORS = [
    ("roi-below", "选区下方（推荐）"),
    ("roi-above", "选区上方"),
    ("roi-over", "覆盖选区"),
    ("bottom-center", "屏幕底部居中"),
    ("top-center", "屏幕顶部居中"),
    ("center", "屏幕正中"),
    ("bottom-left", "屏幕左下"),
    ("bottom-right", "屏幕右下"),
]


# --------------------------------------------------------------------- helpers


def _dspin(
    value: float,
    lo: float,
    hi: float,
    step: float = 0.1,
    suffix: str = "",
    decimals: int = 2,
) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setSingleStep(step)
    # Decimals must be able to represent the current value: with the default of 2,
    # min_change_ratio = 0.004 displayed as 0.00 and every save from this window
    # silently disabled the change gate.
    box.setDecimals(decimals)
    if suffix:
        box.setSuffix(suffix)
    box.setValue(value)
    return box


def _spin(value: int, lo: int, hi: int, suffix: str = "") -> QSpinBox:
    box = QSpinBox()
    box.setRange(lo, hi)
    if suffix:
        box.setSuffix(suffix)
    box.setValue(value)
    return box


def _combo(pairs: list[tuple[str, str]], current: str) -> QComboBox:
    box = QComboBox()
    for key, label in pairs:
        box.addItem(label, key)
    index = box.findData(current)
    box.setCurrentIndex(max(0, index))
    return box


# --------------------------------------------------------------------- preview


class SubtitlePreview(QWidget):
    """Paints one sample subtitle using the *edited* settings.

    Deliberately re-implements the drawing rather than hosting a real
    SubtitleOverlay: that one is a topmost frameless window and cannot be
    embedded. Keep the two in sync if the overlay's look changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(110)
        self._cfg = AppConfig().ui
        self._sample = Subtitle(
            "PlayerOne: need cover, reloading",
            "PlayerOne：掩护我，换弹",
            Source.SCREEN,
            Lang.EN,
        )

    def set_config(self, cfg) -> None:
        self._cfg = cfg
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # Fake a game backdrop so contrast decisions are visible.
        painter.fillRect(self.rect(), QColor(58, 64, 78))
        painter.setPen(QColor(96, 104, 120))
        for y in range(0, self.height(), 28):
            painter.drawLine(0, y, self.width(), y)

        family = self._cfg.font_family or "Sans Serif"
        available = set(QFontDatabase.families())
        if family not in available:
            from .overlay import FONT_STACK

            family = next((f for f in FONT_STACK if f in available), "Sans Serif")
        font = QFont(family)
        font.setPixelSize(max(10, int(self._cfg.font_size)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)

        metrics = QFontMetrics(font)
        text = self._sample.dst_text
        width = metrics.horizontalAdvance(text) + 4
        height = metrics.height()
        x = (self.width() - width) // 2
        y = (self.height() - height) // 2

        if self._cfg.background:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(self._cfg.background_opacity * 255)))
            painter.drawRoundedRect(QRect(x - 10, y - 5, width + 20, height + 10), 6, 6)
        if self._cfg.outline:
            painter.setPen(QPen(QColor(0, 0, 0, 230)))
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1)):
                painter.drawText(x + dx, y + metrics.ascent() + dy, text)
        painter.setPen(QPen(QColor(self._cfg.screen_color)))
        painter.drawText(x, y + metrics.ascent(), text)


# ------------------------------------------------------------------ tab mixin


class _Tab(QWidget):
    def load(self, cfg: AppConfig) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def collect(self, cfg: AppConfig) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def label(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


class RegionsTab(_Tab):
    changed = Signal()

    def __init__(self, on_pick: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._on_pick = on_pick
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "每个区域都会独立跑一次 OCR，成本与区域内文本行数成正比。\n"
                "只框住会变化的文字本身，不要框大。"
            )
        )
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "X", "Y", "宽", "高"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        pick = QPushButton("重新框选…")
        pick.clicked.connect(self._pick)
        self.remove_button = QPushButton("删除选中")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove)
        self.table.itemSelectionChanged.connect(
            lambda: self.remove_button.setEnabled(self.table.currentRow() >= 0)
        )
        self.monitor = _spin(1, 1, 4)
        row.addWidget(pick)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        row.addWidget(QLabel("显示器"))
        row.addWidget(self.monitor)
        layout.addLayout(row)

    def label(self) -> str:
        return "扫描区域"

    def set_pick_handler(self, handler: Callable[[], None] | None) -> None:
        self._on_pick = handler

    def _pick(self) -> None:
        if self._on_pick:
            self._on_pick()

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.changed.emit()

    def load(self, cfg: AppConfig) -> None:
        regions = [r for r in cfg.screen.resolved_regions() if r is not None]
        self.table.setRowCount(len(regions))
        for i, (x, y, w, h) in enumerate(regions):
            for col, value in enumerate((i + 1, x, y, w, h)):
                item = QTableWidgetItem(str(value) if col == 0 else f"{value:.3f}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, col, item)
        self.monitor.setValue(cfg.screen.monitor)

    def collect(self, cfg: AppConfig) -> None:
        regions = []
        for row in range(self.table.rowCount()):
            try:
                regions.append(tuple(float(self.table.item(row, c).text()) for c in range(1, 5)))
            except (AttributeError, TypeError, ValueError):
                continue
        cfg.screen.regions = regions
        cfg.screen.region = None
        cfg.screen.monitor = self.monitor.value()


class TranslateTab(_Tab):
    def __init__(self, on_auth: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._on_auth = on_auth
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.engine = _combo(
            [("auto", "自动（有密钥用 LLM）"), ("llm", "LLM"), ("mock", "Mock（调试）")], "auto"
        )
        self.base_url = QLineEdit()
        self.model = QLineEdit()
        form.addRow("引擎", self.engine)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型", self.model)
        layout.addLayout(form)

        key_row = QHBoxLayout()
        self.key_status = QLabel("…")
        auth = QPushButton("设置密钥…")
        # Resolved at click time, not captured now: the settings window is built
        # before the application's handlers exist, so a captured None meant the
        # button silently did nothing.
        auth.clicked.connect(self._request_key)
        key_row.addWidget(QLabel("密钥"))
        key_row.addWidget(self.key_status, 1)
        key_row.addWidget(auth)
        layout.addLayout(key_row)

        glossary_box = QGroupBox("术语表（优先于模型翻译）")
        g_layout = QVBoxLayout(glossary_box)
        self.glossary = QTableWidget(0, 2)
        self.glossary.setHorizontalHeaderLabels(["原文", "译文"])
        self.glossary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        g_layout.addWidget(self.glossary)
        add = QPushButton("添加一行")
        add.clicked.connect(lambda: self.glossary.insertRow(self.glossary.rowCount()))
        g_layout.addWidget(add)
        layout.addWidget(glossary_box, 1)

    def set_key_handler(self, handler: Callable[[], None] | None) -> None:
        self._on_auth = handler

    def _request_key(self) -> None:
        if self._on_auth is not None:
            self._on_auth()

    def label(self) -> str:
        return "翻译"

    def set_key_status(self, text: str) -> None:
        self.key_status.setText(text)

    def load(self, cfg: AppConfig) -> None:
        tr = cfg.translate
        index = self.engine.findData(tr.engine)
        self.engine.setCurrentIndex(max(0, index))
        self.base_url.setText(tr.base_url)
        self.model.setText(tr.model)
        items = list({**tr.glossary, **tr.glossary_extra}.items())
        self.glossary.setRowCount(len(items))
        for row, (src, dst) in enumerate(items):
            self.glossary.setItem(row, 0, QTableWidgetItem(src))
            self.glossary.setItem(row, 1, QTableWidgetItem(dst))

    def collect(self, cfg: AppConfig) -> None:
        tr = cfg.translate
        tr.engine = self.engine.currentData()
        tr.base_url = self.base_url.text().strip()
        tr.model = self.model.text().strip()
        merged = {}
        for row in range(self.glossary.rowCount()):
            src = self.glossary.item(row, 0)
            dst = self.glossary.item(row, 1)
            if src and dst and src.text().strip() and dst.text().strip():
                merged[src.text().strip()] = dst.text().strip()
        tr.glossary = merged
        tr.glossary_extra = {}


class AppearanceTab(_Tab):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.font_size = _spin(20, 10, 64, " px")
        self.anchor = _combo(ANCHORS, "roi-below")
        self.line_ttl = _dspin(8.0, 1.0, 60.0, 0.5, " s")
        self.fade_in = _spin(220, 0, 2000, " ms")
        self.gap = _spin(12, 0, 200, " px")
        self.max_lines = _spin(4, 1, 10)
        self.width_ratio = _dspin(0.62, 0.2, 1.0, 0.05)
        self.opacity = _dspin(0.55, 0.0, 1.0, 0.05)
        self.outline = QCheckBox("文字描边（强烈建议开启）")
        self.background = QCheckBox("半透明底板")
        self.show_source = QCheckBox("同时显示原文")
        for widget, title in (
            (self.font_size, "字号"),
            (self.anchor, "位置"),
            (self.max_lines, "最多同时显示"),
            (self.line_ttl, "停留时长"),
            (self.fade_in, "淡入时长"),
            (self.gap, "与选区间距"),
            (self.width_ratio, "最大宽度占比"),
            (self.opacity, "底板不透明度"),
        ):
            form.addRow(title, widget)
        form.addRow("", self.outline)
        form.addRow("", self.background)
        form.addRow("", self.show_source)
        layout.addLayout(form)

        layout.addWidget(QLabel("预览（背景模拟游戏画面）"))
        self.preview = SubtitlePreview()
        layout.addWidget(self.preview, 1)
        for widget in (
            self.font_size,
            self.line_ttl,
            self.width_ratio,
            self.opacity,
            self.outline,
            self.background,
        ):
            signal = getattr(widget, "valueChanged", None) or getattr(widget, "toggled", None)
            if signal is not None:
                signal.connect(self._refresh_preview)
        self._cfg = AppConfig()

    def _refresh_preview(self) -> None:
        self.collect(self._cfg)
        self.preview.set_config(self._cfg.ui)

    def label(self) -> str:
        return "外观"

    def load(self, cfg: AppConfig) -> None:
        ui = cfg.ui
        self._cfg = cfg
        self.font_size.setValue(ui.font_size)
        index = self.anchor.findData(ui.anchor)
        self.anchor.setCurrentIndex(max(0, index))
        self.line_ttl.setValue(ui.line_ttl)
        self.fade_in.setValue(ui.fade_in_ms)
        self.gap.setValue(ui.roi_gap_px)
        self.max_lines.setValue(ui.max_lines)
        self.width_ratio.setValue(ui.max_width_ratio)
        self.opacity.setValue(ui.background_opacity)
        self.outline.setChecked(ui.outline)
        self.background.setChecked(ui.background)
        self.show_source.setChecked(ui.show_source_text)
        self.preview.set_config(ui)

    def collect(self, cfg: AppConfig) -> None:
        ui = cfg.ui
        ui.font_size = self.font_size.value()
        ui.anchor = self.anchor.currentData()
        ui.line_ttl = self.line_ttl.value()
        ui.fade_in_ms = self.fade_in.value()
        ui.roi_gap_px = self.gap.value()
        ui.max_lines = self.max_lines.value()
        ui.max_width_ratio = self.width_ratio.value()
        ui.background_opacity = self.opacity.value()
        ui.outline = self.outline.isChecked()
        ui.background = self.background.isChecked()
        ui.show_source_text = self.show_source.isChecked()


class PerformanceTab(_Tab):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.max_fps = _dspin(2.0, 0.2, 30.0, 0.5, " fps")
        self.min_interval = _dspin(1.5, 0.2, 30.0, 0.1, " s")
        self.threads = _spin(1, 1, 16)
        self.rec_height = _spin(32, 24, 64, " px")
        self.min_change = _dspin(0.004, 0.0, 0.2, 0.001, decimals=4)
        self.low_priority = QCheckBox("降低进程优先级（游戏优先）")
        self.keep_on_top = QCheckBox("保持置顶（Windows）")
        for widget, title in (
            (self.max_fps, "抓屏帧率"),
            (self.min_interval, "OCR 最小间隔"),
            (self.threads, "OCR 线程数"),
            (self.rec_height, "识别输入高度"),
            (self.min_change, "变化阈值"),
        ):
            form.addRow(title, widget)
        form.addRow("", self.low_priority)
        form.addRow("", self.keep_on_top)
        layout.addLayout(form)
        layout.addWidget(
            QLabel(
                "线程数保持 1 最省 CPU；识别高度 32 是精度下限，\n"
                "低于 24 会无法识别。改动后需要重启生效。"
            )
        )
        layout.addStretch(1)

    def label(self) -> str:
        return "性能"

    def load(self, cfg: AppConfig) -> None:
        self.max_fps.setValue(cfg.screen.max_fps)
        self.min_interval.setValue(cfg.performance.ocr_min_interval)
        self.threads.setValue(cfg.ocr.threads or 1)
        self.rec_height.setValue(cfg.ocr.rec_height)
        self.min_change.setValue(cfg.screen.min_change_ratio)
        self.low_priority.setChecked(cfg.performance.low_priority)
        self.keep_on_top.setChecked(cfg.ui.keep_on_top)

    def collect(self, cfg: AppConfig) -> None:
        cfg.screen.max_fps = self.max_fps.value()
        cfg.performance.ocr_min_interval = self.min_interval.value()
        cfg.ocr.threads = self.threads.value()
        cfg.ocr.rec_height = self.rec_height.value()
        cfg.screen.min_change_ratio = self.min_change.value()
        cfg.performance.low_priority = self.low_priority.isChecked()
        cfg.ui.keep_on_top = self.keep_on_top.isChecked()


class StatusTab(_Tab):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.summary = QLabel("—")
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addStretch(1)

    def label(self) -> str:
        return "状态"

    def show_snapshot(self, snap: dict) -> None:
        idle = snap.get("idle_for")
        idle_text = f"{idle:.0f}s 前" if isinstance(idle, (int, float)) else "—"
        rows = [
            f"状态：{snap.get('state')}",
            f"已翻译：{snap.get('subtitles')} 条",
            f"最近一条：{idle_text}",
            f"扫描区域：{snap.get('regions')} 个",
            f"模型：{snap.get('model')}",
            "",
            f"抓到的变化帧：{snap.get('frames', '—')}",
            f"OCR 次数：{snap.get('ocr_runs', '—')}",
            f"OCR 平均耗时：{snap.get('ocr_ms', 0):.0f} ms",
            f"翻译耗时：{snap.get('translate_ms', 0):.0f} ms",
            f"新增文本行：{snap.get('lines_new', '—')}",
            f"错误次数：{snap.get('errors', '—')}",
        ]
        cache = snap.get("cache")
        if isinstance(cache, dict) and cache:
            rows += [
                "",
                f"缓存命中：{cache.get('hits', 0):.0f}",
                f"缓存未命中：{cache.get('misses', 0):.0f}",
                f"术语表命中：{cache.get('glossary', 0):.0f}",
                f"命中率：{cache.get('hit_rate', 0) * 100:.0f}%",
            ]
        if snap.get("last_error"):
            rows += ["", f"最近错误：{snap['last_error']}"]
        self.summary.setText("\n".join(rows))

    def load(self, cfg: AppConfig) -> None:  # nothing to edit
        pass

    def collect(self, cfg: AppConfig) -> None:
        pass


# --------------------------------------------------------------------- window


class SettingsWindow(QDialog):
    def __init__(
        self,
        on_pick_regions: Callable[[], None] | None = None,
        on_set_key: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Translator · 设置")
        self.resize(760, 720)

        self.regions = RegionsTab(on_pick_regions)
        self.translate = TranslateTab(on_set_key)
        self.appearance = AppearanceTab()
        self.performance = PerformanceTab()
        self.status = StatusTab()
        self._tabs: list[_Tab] = [
            self.regions,
            self.translate,
            self.appearance,
            self.performance,
            self.status,
        ]

        self.tabs = QTabWidget()
        for tab in self._tabs:
            self.tabs.addTab(tab, tab.label())
        self.regions.changed.connect(self._reload_from_window)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        # Force Chinese: the standard buttons pick up the system locale, which
        # gave an English "Save/Cancel" in an otherwise Chinese window.
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)
        self._cfg = AppConfig()

    def load_config(self, cfg: AppConfig) -> None:
        self._cfg = copy.deepcopy(cfg)
        for tab in self._tabs:
            tab.load(self._cfg)

    def collect_config(self, base: AppConfig | None = None) -> AppConfig:
        """Return a config with the form's edits applied to ``base`` (or the loaded one)."""
        cfg = copy.deepcopy(base if base is not None else self._cfg)
        for tab in self._tabs:
            tab.collect(cfg)
        return cfg

    def _reload_from_window(self) -> None:
        self.load_config(self.collect_config())

    def set_handlers(
        self,
        on_pick_regions: Callable[[], None] | None = None,
        on_set_key: Callable[[], None] | None = None,
    ) -> None:
        """Connect the two buttons that need the application, not the window.

        They cannot be passed to __init__: the window is created first, and the
        application's handlers need the window to exist (they raise dialogs over
        it).
        """
        self.regions.set_pick_handler(on_pick_regions)
        self.translate.set_key_handler(on_set_key)

    def update_status(self, snap: dict) -> None:
        self.status.show_snapshot(snap)

    def set_key_status(self, text: str) -> None:
        self.translate.set_key_status(text)

    def show_tab(self, name: str) -> None:
        index = {"regions": 0, "translate": 1, "appearance": 2, "performance": 3, "status": 4}
        self.tabs.setCurrentIndex(index.get(name, 0))
