"""First-run setup: key, regions, then straight into the overlay.

The goal is that ``translator run`` on a fresh install ends with subtitles on screen,
without the user reading docs or hand-editing TOML. Each step is skipped when it
is already satisfied, so this doubles as a repair path.
"""

from __future__ import annotations

import logging

from ..core.config import AppConfig
from .credentials import (
    MissingApiKey,
    ensure_credentials,
    is_local_endpoint,
    load_api_key,
    load_baidu,
    mask,
)

log = logging.getLogger(__name__)

__all__ = ["SetupResult", "run_setup"]


class SetupResult:
    def __init__(self, ok: bool, message: str = "", changed: bool = False) -> None:
        self.ok = ok
        self.message = message
        self.changed = changed

    def __bool__(self) -> bool:
        return self.ok


def _step_key(cfg: AppConfig, interactive: bool) -> tuple[bool, str]:
    tr = cfg.translate

    from ..translate import resolve_engine

    if resolve_engine(tr) == "baidu":
        appid, secret, source = load_baidu(tr)
        if appid and secret:
            return True, f"百度翻译凭据已就绪（APPID {appid}，来自 {source}）"
        if not interactive:
            return False, "缺少百度翻译 APPID/密钥，且未允许交互输入"
        try:
            ensure_credentials(tr, interactive=True, reason="第一步：配置百度翻译凭据。")
        except MissingApiKey as exc:
            return False, str(exc)
        return True, "百度翻译凭据已保存"

    if is_local_endpoint(tr.base_url):
        return True, "本地模型，无需密钥"
    key, source = load_api_key(tr)
    if key:
        return True, f"密钥已就绪（{mask(key)}，来自 {source}）"
    if not interactive:
        return False, "缺少翻译密钥，且未允许交互输入"
    try:
        ensure_credentials(tr, interactive=True, reason="第一步：配置翻译密钥。")
    except MissingApiKey as exc:
        return False, str(exc)
    return True, "密钥已保存"


def _step_regions(cfg: AppConfig, monitor: int, interactive: bool) -> tuple[bool, str]:
    if cfg.screen.regions or cfg.screen.region is not None:
        count = len(cfg.screen.resolved_regions())
        return True, f"已配置 {count} 个扫描区域"
    if not interactive:
        return True, "未配置区域，将扫描整个显示器（成本较高）"

    from ..ui.roi_picker import pick_regions

    print()
    print("第二步：框选要翻译的区域（可框多个，例如聊天框 + 击杀提示）")
    print("       拖鼠标框选 · Enter 确认 · Backspace 撤销 · Esc 跳过")
    picked = pick_regions(monitor=monitor)
    if not picked:
        print("已跳过；将扫描整个显示器，OCR 成本会明显上升。")
        return True, "未选择区域"
    cfg.screen.regions = [r.fractions for r in picked]
    cfg.screen.region = None
    cfg.screen.monitor = picked[0].monitor
    cfg.screen.monitor_origin = picked[0].monitor_origin
    return True, f"已保存 {len(picked)} 个区域"


def run_setup(
    cfg: AppConfig,
    *,
    interactive: bool = True,
    config_path: str | None = None,
) -> SetupResult:
    """Walk the first-run steps. Returns a result; never raises for user input."""
    changed = False

    ok, message = _step_key(cfg, interactive)
    if not ok:
        return SetupResult(False, message)
    print(f"  ✓ {message}")

    ok, message = _step_regions(cfg, cfg.screen.monitor, interactive)
    if not ok:
        return SetupResult(False, message)
    print(f"  ✓ {message}")
    if "已保存" in message or "未选择" in message:
        changed = True

    if changed:
        path = cfg.save(config_path)
        print(f"  ✓ 配置已写入 {path}")

    return SetupResult(True, "就绪", changed)
