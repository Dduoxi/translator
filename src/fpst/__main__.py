"""CLI entry point: ``python -m fpst`` / ``fpst``.

fpst doctor   # environment + engine availability report (run this first on Windows)
fpst config   # write a commented default config.toml and print its path
fpst roi      # drag a box on screen -> get ROI fractions to paste into config
fpst scan     # capture the configured ROI once and print what OCR sees
fpst watch    # run the always-on scan loop, printing translations
fpst bench    # measure what this will actually cost your game
fpst auth     # store / inspect / remove the translation API key
fpst run      # the actual product: overlay subtitles on screen (--demo to try it)
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from importlib import metadata

from .builtin import MISSING, load_builtin_engines
from .core.config import AppConfig, default_config_path
from .core.interfaces import ASREngine, AudioSource, OCREngine, ScreenSource, Translator
from .core.registry import available
from .core.types import Source

KINDS = {
    "screen": ScreenSource,
    "ocr": OCREngine,
    "audio": AudioSource,
    "asr": ASREngine,
    "translate": Translator,
}

# The promise we make to the user: a background overlay may not cost more than
# this. Enforced by `fpst bench`, achieved by 1-thread OCR + throttling.
BUDGET_CPU = 50.0
"""Percent of ONE core, worst case."""

BUDGET_RSS = 400.0
"""Megabytes of resident memory."""

OPTIONAL_PACKAGES = [
    "mss",
    "opencv-python-headless",
    "rapidocr-onnxruntime",
    "httpx",
    "faster-whisper",
    "sounddevice",
    "PySide6",
]


def _pkg_version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "-"


def cmd_doctor(_args: argparse.Namespace) -> int:
    load_builtin_engines()
    print(f"python   : {sys.version.split()[0]}  ({sys.executable})")
    print(f"platform : {platform.platform()}")
    print(f"config   : {default_config_path()}")
    print()
    print("optional packages:")
    for dist in OPTIONAL_PACKAGES:
        version = _pkg_version(dist)
        print(f"  {'OK ' if version != '-' else '-- '} {dist:<24} {version}")
    print()
    print("registered engines:")
    for label, kind in KINDS.items():
        names = available(kind)
        print(f"  {label:<10} {', '.join(names) if names else '<none>'}")
    if MISSING:
        print()
        print("skipped modules:")
        for module, reason in sorted(MISSING.items()):
            print(f"  {module}: {reason}")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    """Inspect, store or remove the translation API key."""
    from .runtime.credentials import (
        MissingApiKey,
        clear_api_key,
        ensure_api_key,
        is_local_endpoint,
        load_api_key,
        mask,
    )

    cfg = AppConfig.load(args.config)
    tr = cfg.translate
    print(f"endpoint : {tr.base_url}")
    print(f"model    : {tr.model}")

    if is_local_endpoint(tr.base_url):
        print("key      : not required (local endpoint)")
        return 0

    action = args.action or "status"

    if action == "status":
        key, source = load_api_key(tr)
        if not key:
            print("key      : MISSING")
            print()
            print(f"run `fpst auth set` to store one (or set {tr.api_key_env})")
            return 1
        print(f"key      : {mask(key)}  (from {source})")
        if source == "config":
            print("warning  : stored in plaintext in config.toml; prefer `fpst auth set`")
        ok, message = validate_api_key_hint(tr, key, args.verify)
        print(f"check    : {message}")
        return 0 if ok else 1

    if action == "clear":
        removed = clear_api_key()
        print("removed from the credential store" if removed else "nothing stored")
        return 0

    # action == "set"
    try:
        key = ensure_api_key(tr, interactive=True, reason="正在设置翻译密钥。")
    except MissingApiKey as exc:
        print(f"失败：{exc}")
        return 1
    if not key:
        print("此端点无需密钥")
        return 0
    return 0


def validate_api_key_hint(cfg, key: str, do_verify: bool) -> tuple[bool, str]:
    """Only hit the network when the user asked for it (``--verify``)."""
    if not do_verify:
        from .runtime.credentials import mask

        return True, f"{mask(key)} (未校验，加 --verify 可实测)"
    from .runtime.credentials import validate_api_key

    return validate_api_key(key, cfg.base_url, cfg.model, cfg.timeout_s)


def cmd_roi(args: argparse.Namespace) -> int:
    """Pick the ROI with the mouse, optionally verify it and save it."""
    from .capture import make_screen_source
    from .ocr import make_ocr_engine, postprocess
    from .ui.roi_picker import pick_region

    load_builtin_engines()
    cfg = AppConfig.load(args.config)

    picked = pick_region(monitor=args.monitor)
    if picked is None:
        print("cancelled")
        return 1

    print(f"monitor {picked.monitor}  {picked.monitor_size[0]}x{picked.monitor_size[1]} px")
    print(
        f"pixels : {picked.rect_px[0]} {picked.rect_px[1]} {picked.rect_px[2]} {picked.rect_px[3]}"
    )
    print()
    print("paste into config.toml:")
    print("  [screen]")
    print(f"  monitor = {picked.monitor}")
    print(f"  region = {picked.toml_region}")
    print()
    print("or use right now:")
    print(f"  fpst watch --region {picked.cli_region}")

    if args.verify:
        print()
        print("verifying (one OCR pass on the selection)...")
        cfg.screen.monitor = picked.monitor
        cfg.screen.region = picked.fractions
        cfg.screen.min_change_ratio = 0
        source = make_screen_source(cfg.screen)
        try:
            source.open()
            frame = source.grab()
        finally:
            source.close()
        if frame is None:
            print("  no frame captured")
            return 1
        engine = make_ocr_engine(cfg.ocr)
        engine.warmup()
        try:
            started = time.perf_counter()
            regions = engine.recognize(frame)
            elapsed = (time.perf_counter() - started) * 1000
        finally:
            engine.close()
        lines = postprocess(regions, merge=cfg.ocr.merge_lines)
        print(f"  {len(regions)} boxes -> {len(lines)} lines in {elapsed:.0f} ms")
        if not lines:
            print("  nothing recognised -- the box may be empty or too small")
        for line in lines[: cfg.ocr.max_lines]:
            print(f"  [{line.confidence:.2f}] {line.text}")

    if args.write:
        cfg.screen.monitor = picked.monitor
        cfg.screen.region = picked.fractions
        path = cfg.save(args.config)
        print()
        print(f"written to {path}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Always-on scan: prints translated lines as they appear."""
    from .core.bus import EventBus
    from .core.types import Subtitle
    from .pipeline import ScreenPipeline
    from .runtime import apply_resource_policy

    load_builtin_engines()
    cfg = AppConfig.load(args.config)
    if args.region:
        cfg.screen.region = tuple(float(v) for v in args.region)
    if args.fps:
        cfg.screen.max_fps = args.fps

    # Release rule: no key, no service. Fail before touching the screen.
    from .runtime.credentials import MissingApiKey, ensure_api_key

    try:
        ensure_api_key(
            cfg.translate,
            interactive=not args.no_prompt,
            reason="首次使用需要配置翻译密钥。",
        )
    except MissingApiKey as exc:
        print(f"无法启动：{exc}", file=sys.stderr)
        return 2

    # Be a good citizen before doing any work at all.
    for note in apply_resource_policy(
        low_priority=cfg.performance.low_priority,
        cpu_reserve=cfg.performance.cpu_reserve,
    ):
        print(f"resource policy: {note}")

    bus = EventBus()

    def show(sub: Subtitle) -> None:
        stamp = time.strftime("%H:%M:%S")
        latency = f"[{sub.latency_ms:5.0f}ms]" if cfg.ui.show_latency else ""
        print(f"{stamp} {latency} {sub.dst_text}")
        if cfg.ui.show_source_text:
            print(f"            {sub.src_text}")

    bus.subscribe(Subtitle, show)
    pipeline = ScreenPipeline.from_config(cfg, bus)
    print(f"watching at {cfg.screen.max_fps:g} fps, region={cfg.screen.region} (Ctrl-C to stop)")
    started = time.monotonic()
    try:
        pipeline.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.stop()
    elapsed = time.monotonic() - started
    print(f"\n{elapsed:.0f}s: {pipeline.stats.summary()}")
    return 0


def _cpu_ms(fn, repeats: int = 3) -> tuple[float, float]:
    """Return (best wall-clock ms, best CPU ms) for ``fn``."""
    best_cpu = float("inf")
    best_wall = float("inf")
    for _ in range(repeats):
        wall0 = time.perf_counter()
        cpu0 = time.process_time()
        fn()
        wall = (time.perf_counter() - wall0) * 1000
        cpu = (time.process_time() - cpu0) * 1000
        best_wall = min(best_wall, wall)
        best_cpu = min(best_cpu, cpu)
    return best_wall, best_cpu


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return 0.0


_DEMO_LINES = [
    (
        "PlayerOne: rush B site now, no one watching mid",
        "PlayerOne：快攻B点，中路没人",
        Source.SCREEN,
    ),
    ("xX_Shadow_Xx: gg wp", "xX_Shadow_Xx：打得好", Source.SCREEN),
    ("TeamMate: enemy behind the box, two hp", "TeamMate：箱子后敌人，残血", Source.SCREEN),
    ("enemy pushing A main, need backup", "敌人压A大，快来支援", Source.VOICE),
    ("i have ult, ready when you are", "我有大招，随时可以上", Source.VOICE),
    ("PlayerOne: need cover, reloading", "PlayerOne：掩护我，换弹", Source.SCREEN),
]


def cmd_run(args: argparse.Namespace) -> int:
    """The product: capture -> OCR -> translate -> transparent overlay."""
    from PySide6.QtCore import QTimer

    from .core.bus import EventBus
    from .core.types import Lang, Subtitle
    from .runtime import apply_resource_policy
    from .runtime.credentials import MissingApiKey, ensure_api_key
    from .ui.overlay import OverlayController, SubtitleOverlay, create_app

    load_builtin_engines()
    cfg = AppConfig.load(args.config)
    if args.region:
        cfg.screen.region = tuple(float(v) for v in args.region)

    if not args.demo:
        try:
            ensure_api_key(
                cfg.translate,
                interactive=not args.no_prompt,
                reason="首次使用需要配置翻译密钥。",
            )
        except MissingApiKey as exc:
            print(f"无法启动：{exc}", file=sys.stderr)
            return 2
        for note in apply_resource_policy(
            low_priority=cfg.performance.low_priority,
            cpu_reserve=cfg.performance.cpu_reserve,
        ):
            print(f"resource policy: {note}")

    app = create_app()
    overlay = SubtitleOverlay(cfg.ui, monitor=cfg.screen.monitor, roi=cfg.screen.region)
    overlay.show()

    bus = EventBus()
    controller = OverlayController(overlay, bus)

    pipeline = None
    demo_timer = None
    if args.demo:
        state = {"i": 0}

        def feed() -> None:
            src, dst, source = _DEMO_LINES[state["i"] % len(_DEMO_LINES)]
            state["i"] += 1
            bus.publish(
                Subtitle(
                    src_text=src,
                    dst_text=dst,
                    source=source,
                    src_lang=Lang.EN,
                    latency_ms=880.0,
                )
            )
            app.processEvents()  # noqa: F841 - keep the demo lively without a pipeline

        demo_timer = QTimer()
        demo_timer.timeout.connect(feed)
        demo_timer.start(2200)
        print("demo mode: showing sample subtitles (Ctrl-C to stop)")
    else:
        from .pipeline import ScreenPipeline

        pipeline = ScreenPipeline.from_config(cfg, bus)
        pipeline.start()
        print(
            f"running: region={cfg.screen.region} at {cfg.screen.max_fps:g} fps, "
            f"model={cfg.translate.model} (Ctrl-C to stop)"
        )

    # Let Ctrl-C actually reach us: Qt's event loop blocks Python signal handling
    # unless something wakes it periodically.
    import signal

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    wakeup = QTimer()
    wakeup.timeout.connect(lambda: None)
    wakeup.start(250)

    try:
        code = app.exec()
    finally:
        if demo_timer is not None:
            demo_timer.stop()
        if pipeline is not None:
            pipeline.stop()
            print(f"\n{pipeline.stats.summary()}")
        controller.close()
    return int(code)


def cmd_bench(args: argparse.Namespace) -> int:
    """Measure the real cost, then compare it against the budget."""
    import os

    from .capture import make_screen_source
    from .ocr import make_ocr_engine

    load_builtin_engines()
    cfg = AppConfig.load(args.config)
    if args.region:
        cfg.screen.region = tuple(float(v) for v in args.region)
    cfg.screen.min_change_ratio = 0

    cores = os.cpu_count() or 1
    print(f"machine  : {cores} logical cores, rss {_rss_mb():.0f} MB")
    print(f"budget   : <= {BUDGET_CPU:.0f}% of one core, <= {BUDGET_RSS:.0f} MB")
    print()

    # ---- capture ---------------------------------------------------------
    source = make_screen_source(cfg.screen)
    source.open()
    try:
        width, height = source.size
        grab_wall, grab_cpu = _cpu_ms(source.grab, repeats=8)
        frame = source.grab()
    finally:
        source.close()

    print(f"capture  : {width}x{height} roi={cfg.screen.region}")
    print(f"  grab {grab_wall:6.1f} ms wall  {grab_cpu:6.1f} ms cpu  (engine: {cfg.screen.engine})")
    if frame is None:
        print("  no frame captured")
        return 1

    # ---- ocr -------------------------------------------------------------
    engine = make_ocr_engine(cfg.ocr)
    started = time.perf_counter()
    engine.warmup()
    warmup_s = time.perf_counter() - started
    rss_loaded = _rss_mb()
    try:
        ocr_wall, ocr_cpu = _cpu_ms(lambda: engine.recognize(frame), repeats=args.repeats)
        regions = engine.recognize(frame)
    finally:
        engine.close()

    print(
        f"  ocr  {ocr_wall:6.1f} ms wall  {ocr_cpu:6.1f} ms cpu  "
        f"({len(regions)} boxes, threads={cfg.ocr.threads or 'auto'})"
    )
    print(f"  model load {warmup_s:.2f}s, rss {rss_loaded:.0f} MB")

    # ---- budget ----------------------------------------------------------
    interval = max(0.1, cfg.performance.ocr_min_interval)
    capture_pct = grab_cpu * cfg.screen.max_fps / 10.0  # cpu_ms * fps / 1000 * 100
    ocr_pct = ocr_cpu / (interval * 1000) * 100
    total_pct = capture_pct + ocr_pct

    print()
    print("capture is always-on; OCR only runs when the ROI actually changes:")
    print(
        f"  capture  {capture_pct:5.1f}% of a core   ({cfg.screen.max_fps:g} fps x {grab_cpu:.1f} ms)"
    )
    print()
    print("  message rate     OCR only     total CPU")
    for label, every in (("1 per 10 s", 10.0), ("1 per 5 s", 5.0), ("1 per 2 s", 2.0)):
        msg_pct = ocr_cpu / (every * 1000) * 100
        print(f"  {label:13}  {msg_pct:6.1f}%      {capture_pct + msg_pct:6.1f}% of a core")
    print(
        f"  {'saturated':13}  {ocr_pct:6.1f}%      {total_pct:6.1f}% of a core"
        "   <- worst case (throttle-bound)"
    )
    print()
    print(f"  machine-wide CPU  {total_pct / cores:5.1f}%   memory {rss_loaded:.0f} MB")
    print()

    problems = []
    if total_pct > BUDGET_CPU:
        problems.append(f"cpu {total_pct:.0f}% > {BUDGET_CPU:.0f}% of one core")
    if rss_loaded > BUDGET_RSS:
        problems.append(f"memory {rss_loaded:.0f} MB > {BUDGET_RSS:.0f} MB")
    if problems:
        print("OVER BUDGET: " + "; ".join(problems))
        print("  -> shrink screen.region, raise performance.ocr_min_interval,")
        print("     or keep [ocr] threads = 1")
    else:
        print("within budget")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = AppConfig()
    path = cfg.save(args.path)
    print(path)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Capture once and OCR it -- the fastest way to tune a ROI."""
    from .capture import make_screen_source
    from .ocr import make_ocr_engine, postprocess

    load_builtin_engines()
    cfg = AppConfig.load(args.config)
    if args.region:
        cfg.screen.region = tuple(float(v) for v in args.region)
    cfg.screen.min_change_ratio = 0  # always get a frame on a one-shot scan

    source = make_screen_source(cfg.screen)
    try:
        source.open()
        width, height = source.size
        print(f"capture: {width}x{height} region={cfg.screen.region}")
        frame = source.grab()
    finally:
        source.close()

    if frame is None:
        print("no frame captured")
        return 1

    if args.save:
        import numpy as np

        np.save(args.save, frame.image)
        print(f"frame saved to {args.save}")

    engine = make_ocr_engine(cfg.ocr)
    engine.warmup()
    try:
        started = time.perf_counter()
        regions = engine.recognize(frame)
        elapsed = (time.perf_counter() - started) * 1000
    finally:
        engine.close()

    lines = postprocess(regions, merge=cfg.ocr.merge_lines)[: cfg.ocr.max_lines]
    print(f"ocr: {len(regions)} boxes -> {len(lines)} lines in {elapsed:.0f} ms")
    print("-" * 60)
    for line in lines:
        print(f"[{line.confidence:.2f}] {line.text}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fpst", description="in-game FPS translator")
    sub = parser.add_subparsers(dest="command")

    p_doctor = sub.add_parser("doctor", help="report environment and available engines")
    p_doctor.set_defaults(func=cmd_doctor)

    p_config = sub.add_parser("config", help="write a default config.toml")
    p_config.add_argument("--path", default=None, help="override target path")
    p_config.set_defaults(func=cmd_config)

    p_roi = sub.add_parser("roi", help="drag a box to pick the ROI, print fractions")
    p_roi.add_argument("--config", default=None, help="config file (used by --write)")
    p_roi.add_argument("--monitor", type=int, default=1, help="which monitor to overlay (1-based)")
    p_roi.add_argument("--verify", action="store_true", help="OCR the selection immediately")
    p_roi.add_argument("--write", action="store_true", help="save the selection into config.toml")
    p_roi.set_defaults(func=cmd_roi)

    p_scan = sub.add_parser("scan", help="capture the ROI once and print OCR output")
    p_scan.add_argument("--config", default=None, help="config file to load")
    p_scan.add_argument(
        "--region",
        nargs=4,
        metavar=("X", "Y", "W", "H"),
        help="ROI as fractions of the monitor, e.g. 0 0.55 1 0.45",
    )
    p_scan.add_argument("--save", default=None, help="write the captured frame as .npy")
    p_scan.set_defaults(func=cmd_scan)

    p_watch = sub.add_parser("watch", help="scan continuously and print translations")
    p_watch.add_argument("--config", default=None, help="config file to load")
    p_watch.add_argument(
        "--region", nargs=4, metavar=("X", "Y", "W", "H"), help="ROI as monitor fractions"
    )
    p_watch.add_argument(
        "--fps", type=float, default=None, help="capture rate (default from config)"
    )
    p_watch.add_argument(
        "--no-prompt", action="store_true", help="never ask for a key; fail instead"
    )
    p_watch.set_defaults(func=cmd_watch)

    p_run = sub.add_parser("run", help="overlay translated subtitles on screen")
    p_run.add_argument("--config", default=None, help="config file to load")
    p_run.add_argument(
        "--region", nargs=4, metavar=("X", "Y", "W", "H"), help="ROI as monitor fractions"
    )
    p_run.add_argument("--demo", action="store_true", help="show sample subtitles, no capture")
    p_run.add_argument("--no-prompt", action="store_true", help="never ask for a key")
    p_run.set_defaults(func=cmd_run)

    p_auth = sub.add_parser("auth", help="store / inspect / remove the API key")
    p_auth.add_argument("action", nargs="?", choices=["status", "set", "clear"], default="status")
    p_auth.add_argument("--config", default=None, help="config file to load")
    p_auth.add_argument("--verify", action="store_true", help="make a real request to check")
    p_auth.set_defaults(func=cmd_auth)

    p_bench = sub.add_parser("bench", help="measure CPU/memory cost against the budget")
    p_bench.add_argument("--config", default=None, help="config file to load")
    p_bench.add_argument("--region", nargs=4, metavar=("X", "Y", "W", "H"), help="ROI to measure")
    p_bench.add_argument("--repeats", type=int, default=3, help="OCR timing repeats")
    p_bench.set_defaults(func=cmd_bench)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Line-buffer even when piped to a file/tee, so `fpst watch > log` streams.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
