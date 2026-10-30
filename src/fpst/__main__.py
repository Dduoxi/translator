"""CLI entry point: ``python -m fpst`` / ``fpst``.

fpst doctor   # environment + engine availability report (run this first on Windows)
fpst config   # write a commented default config.toml and print its path
"""

from __future__ import annotations

import argparse
import platform
import sys
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fpst", description="in-game FPS translator")
    sub = parser.add_subparsers(dest="command")

    p_doctor = sub.add_parser("doctor", help="report environment and available engines")
    p_doctor.set_defaults(func=cmd_doctor)

    p_config = sub.add_parser("config", help="write a default config.toml")
    p_config.add_argument("--path", default=None, help="override target path")
    p_config.set_defaults(func=cmd_config)
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
