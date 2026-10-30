"""CLI entry point: ``python -m fpst`` / ``fpst``.

fpst doctor   # environment + engine availability report (run this first on Windows)
fpst config   # write a commented default config.toml and print its path
fpst scan     # capture the configured ROI once and print what OCR sees
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

KINDS = {
    "screen": ScreenSource,
    "ocr": OCREngine,
    "audio": AudioSource,
    "asr": ASREngine,
    "translate": Translator,
}

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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
