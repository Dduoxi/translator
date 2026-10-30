"""Credential entry for the GUI.

The CLI prompt is useless in a packaged build (no stdin), so the settings window
needs a real dialog. It must also *validate before saving*: a wrong key or a
mismatched Baidu appid/secret should be caught here, not at the first subtitle.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.config import TranslateConfig
from ..runtime.credentials import (
    load_api_key,
    load_baidu,
    mask,
    save_api_key,
    save_baidu,
    validate_api_key,
    validate_baidu,
)

log = logging.getLogger(__name__)

__all__ = ["CredentialDialog"]

PROVIDER_LLM = "llm"
PROVIDER_BAIDU = "baidu"


class CredentialDialog(QDialog):
    """Ask for, test and store the credentials for one translation provider."""

    def __init__(self, cfg: TranslateConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.saved = False
        self._tested_ok = False
        self.setWindowTitle("设置翻译凭据")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)

        self.provider = QComboBox()
        self.provider.addItem("模型翻译（OpenAI 兼容，如 DeepSeek）", PROVIDER_LLM)
        self.provider.addItem("百度翻译", PROVIDER_BAIDU)
        current = PROVIDER_BAIDU if cfg.engine == "baidu" else PROVIDER_LLM
        self.provider.setCurrentIndex(max(0, self.provider.findData(current)))
        self.provider.currentIndexChanged.connect(self._provider_changed)

        self.form = QFormLayout()
        self.endpoint_label = QLabel()
        self.endpoint_label.setWordWrap(True)
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.field_a = QLineEdit()
        self.field_b = QLineEdit()
        self.field_b.setEchoMode(QLineEdit.EchoMode.Password)
        self.label_a = QLabel()
        self.label_b = QLabel()

        self.form.addRow("服务商", self.provider)
        self.form.addRow("接口", self.endpoint_label)
        self.form.addRow(self.label_a, self.field_a)
        self.form.addRow(self.label_b, self.field_b)
        layout.addLayout(self.form)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.test_button = buttons.addButton("测试连接", QDialogButtonBox.ButtonRole.ActionRole)
        self.save_button = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.test_button.clicked.connect(self._on_test)
        self.save_button.clicked.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync_provider()

    # ------------------------------------------------------------------ setup
    def _provider_changed(self) -> None:
        self._tested_ok = False
        self._sync_provider()

    def _provider(self) -> str:
        return self.provider.currentData() or PROVIDER_LLM

    def _sync_provider(self) -> None:
        """Switch labels, hidden fields and prefill to the chosen provider."""
        if self._provider() == PROVIDER_BAIDU:
            appid, secret, source = load_baidu(self._cfg)
            self.endpoint_label.setText("https://api.fanyi.baidu.com")
            self.label_a.setText("APPID")
            self.label_b.setText("密钥")
            self.field_a.setPlaceholderText("纯数字，如 2024010100000001")
            self.field_b.setPlaceholderText("")
            self.field_a.setText(appid)
            self.field_b.setText(secret)
            self._set_status(
                f"已保存：APPID {appid}（来自 {source}）" if appid else "尚未配置百度凭据",
                ok=bool(appid),
            )
        else:
            key, source = load_api_key(self._cfg)
            self.endpoint_label.setText(f"{self._cfg.base_url}    模型 {self._cfg.model}")
            self.label_a.setText("API Key")
            self.label_b.setText("确认")
            self.field_a.setPlaceholderText("sk-...")
            self.field_b.setPlaceholderText("再输入一次以防打错")
            self.field_a.setText(key)
            self.field_b.setText(key)
            self.field_a.setEchoMode(QLineEdit.EchoMode.Password)
            self._set_status(
                f"已保存：{mask(key)}（来自 {source}）" if key else "尚未配置模型密钥",
                ok=bool(key),
            )

    def _set_status(self, text: str, ok: bool | None = None) -> None:
        colour = {True: "#3ddc84", False: "#ff9e6b", None: "#c8ccd4"}[ok]
        self.status.setText(f"<span style='color:{colour}'>{text}</span>")

    # ------------------------------------------------------------------ tests
    def _values(self) -> tuple[str, str] | None:
        a = self.field_a.text().strip()
        b = self.field_b.text().strip()
        if not a:
            self._set_status("请先填写", ok=False)
            return None
        if self._provider() == PROVIDER_LLM and a != b:
            self._set_status("两次输入的 Key 不一致", ok=False)
            return None
        return a, b

    def _on_test(self) -> None:
        values = self._values()
        if values is None:
            return
        a, b = values
        self._set_status("正在测试…")
        self.test_button.setEnabled(False)
        try:
            if self._provider() == PROVIDER_BAIDU:
                ok, message = validate_baidu(a, b)
            else:
                ok, message = validate_api_key(a, self._cfg.base_url, self._cfg.model)
        finally:
            self.test_button.setEnabled(True)
        self._tested_ok = ok
        self._set_status(message, ok=ok)

    def _on_save(self) -> None:
        values = self._values()
        if values is None:
            return
        a, b = values
        # Never store something we have not proved works: a typo here would only
        # surface as "no subtitles" later, which is far harder to diagnose.
        if not self._tested_ok:
            self._on_test()
            if not self._tested_ok:
                return
        if self._provider() == PROVIDER_BAIDU:
            method = save_baidu(a, b)
            self._cfg.baidu_appid = a if method == "config" else ""
            self._cfg.baidu_key = b if method == "config" else ""
            self._cfg.engine = "baidu"
        else:
            method = save_api_key(a)
            if method == "config":
                self._cfg.api_key = a
            # Leave "auto" alone: it already prefers the model translator.
            if self._cfg.engine == "baidu":
                self._cfg.engine = "auto"
        where = "系统凭据管理器" if method == "keyring" else "配置文件"
        log.info("credentials saved to %s", where)
        self.saved = True
        self.accept()
