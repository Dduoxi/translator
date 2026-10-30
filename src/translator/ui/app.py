"""Assemble the product: overlay + pipeline + tray + settings window.

This is what ``translator run`` starts. Keeping the wiring in one place means the CLI
stays a thin argument parser and the whole thing can be driven headlessly in
tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QSystemTrayIcon

from ..core.config import AppConfig, default_config_path
from ..runtime.credentials import (
    MissingApiKey,
    ensure_credentials,
    load_api_key,
    mask,
)
from .controller import AppController, RunState, StatusPoller
from .overlay import OverlayController, SubtitleOverlay, create_app
from .settings_window import SettingsWindow
from .tray import TrayIcon

log = logging.getLogger(__name__)

__all__ = ["run_app"]


def _key_status(cfg: AppConfig) -> str:
    from ..runtime.credentials import is_local_endpoint, load_baidu

    if cfg.translate.engine == "baidu" or cfg.translate.engine == "auto":
        appid, secret, source = load_baidu(cfg.translate)
        if appid and secret:
            return f"百度 APPID {appid}（{source}）"
        if cfg.translate.engine == "baidu":
            return "百度凭据未配置"
    if is_local_endpoint(cfg.translate.base_url):
        return "本地模型，无需密钥"
    key, source = load_api_key(cfg.translate)
    return f"{mask(key)}（{source}）" if key else "未配置"


_DEMO = [
    ("PlayerOne: rush B site now", "PlayerOne：快攻B点", "screen"),
    ("xX_Shadow_Xx: gg wp", "xX_Shadow_Xx：打得好", "screen"),
    ("enemy pushing A main", "敌人压A大", "voice"),
    ("TeamMate: enemy behind the box, two hp", "TeamMate：箱子后敌人，残血", "screen"),
]


def run_app(
    config_path: str | Path | None = None,
    *,
    show_settings: bool = False,
    demo: bool = False,
    interactive: bool = True,
) -> int:
    """Build everything and hand control to Qt. Returns the process exit code."""
    path = Path(config_path) if config_path else default_config_path()
    app = create_app()
    app.setApplicationName("Translator")

    controller = AppController(path)
    cfg = controller.cfg

    # A missing credential is a configuration problem, not a reason to refuse to
    # start: the settings window is where the user fixes it. Aborting here is
    # exactly what made the packaged build look like it did nothing at all --
    # a windowed build has no stdin, so the interactive prompt threw and the
    # process exited before any window appeared.
    key_warning = ""
    if not demo:
        try:
            ensure_credentials(
                cfg.translate,
                interactive=interactive,
                reason="首次使用需要配置翻译凭据。",
            )
        except MissingApiKey as exc:
            key_warning = str(exc)
            log.warning("starting without credentials: %s", exc)

    overlay = SubtitleOverlay(
        cfg.ui,
        monitor=cfg.screen.monitor,
        rois=cfg.screen.resolved_regions(),
        origin=cfg.screen.monitor_origin,
    )
    overlay.show()
    controller.attach_overlay(overlay)
    bus_controller = OverlayController(overlay, controller.bus)

    window = SettingsWindow()
    window.load_config(cfg)
    window.set_key_status(_key_status(cfg))

    def refresh_status() -> None:
        snap = controller.snapshot()
        window.update_status(snap)
        if tray is not None:
            tray.set_state(controller.state)
            tray.set_status(controller.status_text())

    poller = StatusPoller(controller, lambda _snap: refresh_status())

    def open_settings() -> None:
        window.load_config(controller.cfg)
        window.set_key_status(_key_status(controller.cfg))
        refresh_status()
        window.show()
        window.raise_()
        window.activateWindow()

    def pick_regions() -> None:
        from .roi_picker import pick_regions as pick

        picked = pick(
            monitor=controller.cfg.screen.monitor,
            origin=controller.cfg.screen.monitor_origin,
        )
        if not picked:
            return
        config = controller.cfg
        config.screen.regions = [r.fractions for r in picked]
        config.screen.region = None
        config.screen.monitor = picked[0].monitor
        config.screen.monitor_origin = picked[0].monitor_origin
        controller.apply_and_restart(config)
        window.load_config(controller.cfg)
        if tray is not None:
            tray.notify("已更新区域", f"共 {len(picked)} 个扫描区域")

    def set_key() -> None:
        """Ask for credentials in a dialog -- the CLI prompt cannot work here."""
        from .key_dialog import CredentialDialog

        dialog = CredentialDialog(controller.cfg.translate, parent=window)
        if dialog.exec() != CredentialDialog.DialogCode.Accepted or not dialog.saved:
            return
        # Persist: the dialog may have written appid/key into the config when the
        # OS credential store was unavailable, and it may have switched engine.
        controller.apply_and_restart(controller.cfg)
        window.load_config(controller.cfg)
        window.set_key_status(_key_status(controller.cfg))
        if tray is not None:
            tray.notify("凭据已更新", _key_status(controller.cfg))

    def save_settings() -> None:
        edited = window.collect_config(controller.cfg)
        controller.apply_and_restart(edited)
        window.set_key_status(_key_status(controller.cfg))
        refresh_status()

    def quit_app() -> None:
        controller.shutdown()
        bus_controller.close()
        overlay.close()
        app.quit()

    tray: TrayIcon | None = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TrayIcon(
            on_toggle=controller.toggle,
            on_settings=open_settings,
            on_pick_regions=pick_regions,
            on_set_key=set_key,
            on_quit=quit_app,
        )
    else:
        log.info("no system tray available; running with the overlay only")

    # The window is built before these handlers exist, so wire them afterwards.
    window.set_handlers(on_pick_regions=pick_regions, on_set_key=set_key)
    window.accepted.connect(save_settings)

    demo_timer = None
    if demo:
        from ..core.types import Lang, Source, Subtitle

        state = {"i": 0}

        def feed() -> None:
            src, dst, kind = _DEMO[state["i"] % len(_DEMO)]
            state["i"] += 1
            source = Source.VOICE if kind == "voice" else Source.SCREEN
            controller.bus.publish(Subtitle(src, dst, source, Lang.EN, latency_ms=900.0))

        demo_timer = QTimer()
        demo_timer.timeout.connect(feed)
        demo_timer.start(2200)
    else:
        controller.start()
        if controller.state == RunState.ERROR and tray is not None:
            tray.notify("启动失败", controller.status_text())
    poller.start()
    refresh_status()

    if key_warning:
        window.show_tab("translate")
        window.set_key_status(key_warning)
        if tray is not None:
            tray.notify("还没配置翻译凭据", "在设置窗口里点「设置密钥…」")

    if show_settings or demo or key_warning:
        open_settings()
    elif tray is not None:
        tray.notify("Translator 已启动", "单击托盘图标打开设置，中键暂停")

    # Qt blocks Python signal handling unless something wakes the loop.
    import signal

    signal.signal(signal.SIGINT, lambda *_: quit_app())
    wakeup = QTimer()
    wakeup.timeout.connect(lambda: None)
    wakeup.start(250)

    try:
        return int(app.exec())
    finally:
        if demo_timer is not None:
            demo_timer.stop()
        poller.stop()
        controller.shutdown()
        bus_controller.close()
        if tray is not None:
            tray.hide()
