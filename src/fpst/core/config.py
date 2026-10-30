"""Configuration tree with TOML persistence.

The file is meant to be hand-edited by users (we ship a commented default), so
the schema is flat and forgiving: unknown keys are ignored, missing keys fall
back to defaults.
"""

from __future__ import annotations

import os
import sys
import tomllib
import types
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

__all__ = [
    "ScreenConfig",
    "OCRConfig",
    "AudioConfig",
    "ASRConfig",
    "TranslateConfig",
    "UIConfig",
    "AppConfig",
    "default_config_path",
    "app_data_dir",
]

# FPS-specific vocab that generic MT models routinely mangle.
DEFAULT_GLOSSARY: dict[str, str] = {
    "rush": "快攻",
    "push": "压上",
    "flank": "侧翼包抄",
    "rotate": "转点",
    "peek": "探头对枪",
    "hold": "守住",
    "cover me": "掩护我",
    "on my mark": "听我口令",
    "gg": "打得好",
    "wp": "干得漂亮",
    "nt": "可惜了",
    "eco": "经济局",
    "one hp": "残血一个",
    "trading": "换人",
}


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "FpsTranslator"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "fps-translator"


def default_config_path() -> Path:
    return app_data_dir() / "config.toml"


@dataclass
class ScreenConfig:
    engine: str = "auto"
    """auto | mss | dxgi"""
    monitor: int = 1
    """1-based monitor index, matching what Windows' display settings show."""
    region: tuple[float, float, float, float] | None = None
    """ROI as fractions of the monitor: (x, y, w, h). None = full monitor."""
    max_fps: float = 2.0
    """Capture poll rate. Chat is not a live feed -- messages arrive every few
    seconds at best -- so 2 Hz bounds detection latency at 500 ms, which is
    imperceptible for a message that itself appeared at a random moment.
    Grabbing is cheap on DXGI (~1 ms), so Windows users can raise this to 10-15
    for a snappier feel; on mss each grab costs 9-30 ms, so keep it <= 4."""
    settle_ms: float = 250.0
    """Debounce: after a change, wait until the ROI has been *unchanged* for this
    long before running OCR.

    Chat text fades/scrolls in over a few frames. OCRing mid-animation reads
    half-rendered glyphs and burns several runs on one message. Waiting for a
    quarter second of stillness gives clean text and one run instead of five.
    Set to 0 to OCR the first changed frame immediately."""
    min_change_ratio: float = 0.004
    """Fraction of changed pixels needed to bother running OCR."""


@dataclass
class OCRConfig:
    engine: str = "rapidocr"
    lang: str = "en"
    min_confidence: float = 0.55
    merge_lines: bool = True
    """Join nearby boxes into sentences (chat wraps across lines)."""
    max_lines: int = 12
    threads: int = 0
    """onnxruntime intra-op threads. 0 = auto (cpu_count//2, capped at 8)."""
    det_limit_side: int = 960
    rec_batch: int = 1
    """Keep at 1: batching pads every line to the longest one and is slower here."""
    rec_height: int = 32
    """Recogniser input height. 32 is 2.3x cheaper than the stock 48 at equal
    accuracy; dropping to 24 breaks recognition. Cost is quadratic in this."""


@dataclass
class AudioConfig:
    engine: str = "device_loopback"
    """device_loopback | process_loopback"""
    device: str | None = None
    """Substring match on the playback device name. None = system default."""
    process: str = "Discord.exe"
    """Target executable for Windows process loopback (Win10 2004+)."""
    segmenter: str = "energy"
    """energy | webrtc | silero"""
    target_sample_rate: int = 16000


@dataclass
class ASRConfig:
    engine: str = "faster_whisper"
    model: str = "small.en"
    device: str = "auto"
    compute_type: str = "int8"
    beam_size: int = 1
    min_speech_seconds: float = 0.3
    max_utterance_seconds: float = 12.0


@dataclass
class TranslateConfig:
    engine: str = "mock"
    src_lang: str = "en"
    dst_lang: str = "zh"
    cache_size: int = 1024
    timeout_s: float = 4.0
    glossary: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_GLOSSARY))


@dataclass
class UIConfig:
    font_family: str = ""
    font_size: int = 20
    opacity: float = 0.88
    max_lines: int = 4
    max_width_ratio: float = 0.62
    anchor: str = "bottom-center"
    """bottom-center | top-center | bottom-left | ..."""
    margin_px: int = 64
    show_source_text: bool = False
    show_latency: bool = True
    screen_color: str = "#e8f0ff"
    voice_color: str = "#ffe9a8"


@dataclass
class PerformanceConfig:
    """The "do not hurt the game" knobs.

    A background overlay shares the machine with something that needs every
    core. These defaults favour low resource use over low latency; the budget
    they enforce is documented in the README and measured by ``fpst bench``.
    """

    ocr_min_interval: float = 1.5
    """Minimum seconds between two OCR runs. This is the real throttle: capture
    stays fast (it is cheap) while recognition is spaced out."""
    low_priority: bool = True
    """Drop this process below normal priority so the game always preempts us."""
    idle_timeout: float = 0.0
    """After this many seconds with no change, poll at 1 Hz instead of max_fps.
    0 = never idle back off."""
    cpu_reserve: int = 0
    """Leave this many logical cores unclaimed (0 = use the OS default).
    Best-effort affinity hint, mainly useful on Windows."""


@dataclass
class AppConfig:
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: Path | str | None = None) -> AppConfig:
        path = Path(path) if path is not None else default_config_path()
        if not path.exists():
            return cls()
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        cfg = cls()
        for section_name, section in raw.items():
            target = getattr(cfg, section_name, None)
            if not is_dataclass(target) or not isinstance(section, dict):
                continue
            hints = _hints(type(target))
            for key, value in section.items():
                annotation = hints.get(key)
                if annotation is None:
                    continue  # unknown key
                setattr(target, key, _coerce(annotation, getattr(target, key), value))
        return cfg

    def to_toml(self) -> str:
        return _dump_toml(asdict(self))

    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path) if path is not None else default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_toml(), encoding="utf-8")
        return path


_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    if cls not in _HINTS:
        _HINTS[cls] = get_type_hints(cls)
    return _HINTS[cls]


def _coerce(annotation: Any, current: Any, value: Any) -> Any:
    """Cast a TOML value to the field's declared type, using the annotation.

    Done by annotation rather than by ``type(current)`` because defaults are
    often ``None`` (e.g. ``region``), which carries no type information.
    """
    if value is None:
        return None
    origin = get_origin(annotation)

    # X | None  ->  recurse into X
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _coerce(non_none[0], current, value)
        return value

    if origin in (tuple, list):
        return tuple(value) if isinstance(value, (list, tuple)) else value
    if annotation is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if annotation is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if annotation is int and isinstance(value, float):
        return int(value)
    if annotation is str and not isinstance(value, str):
        return str(value)
    return value


_BARE_KEY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _fmt_key(key: str) -> str:
    """Bare TOML keys are [A-Za-z0-9_-]+; anything else must be quoted.

    Glossary keys like "cover me" and "on my mark" contain spaces -- writing
    them bare produces a file that tomllib refuses to parse.
    """
    if key and all(ch in _BARE_KEY for ch in key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fmt_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt_scalar(v) for v in value) + "]"
    return _fmt_scalar(str(value))


def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    scalar_items = {k: v for k, v in data.items() if not isinstance(v, dict)}
    table_items = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, value in scalar_items.items():
        lines.append(f"{_fmt_key(key)} = {_fmt_scalar(value)}")
    for section, table in table_items.items():
        lines.append("")
        lines.append(f"[{section}]")
        flat = {k: v for k, v in table.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in table.items() if isinstance(v, dict)}
        for key, value in flat.items():
            lines.append(f"{_fmt_key(key)} = {_fmt_scalar(value)}")
        for sub, subtable in nested.items():
            lines.append("")
            lines.append(f"[{section}.{sub}]")
            for key, value in subtable.items():
                lines.append(f"{_fmt_key(key)} = {_fmt_scalar(value)}")
    return "\n".join(lines) + "\n"
