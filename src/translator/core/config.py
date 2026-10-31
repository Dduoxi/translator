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
    "FilterConfig",
    "AppConfig",
    "default_config_path",
    "app_data_dir",
]

# Cross-genre gaming vocabulary.
#
# Kept genre-neutral on purpose: `push` means "推塔" in Dota but "压上" in CS, so
# it is rendered generically ("推进") and users refine it per game via
# [translate.context] or [translate.glossary_extra].
#
# Deliberately excluded: single ambiguous letters ("b" = 回城 would poison every
# mention of "B site").
DEFAULT_GLOSSARY: dict[str, str] = {
    # -- universal chatter / politeness -------------------------------------
    "gg": "打得好",
    "ggwp": "打得好",
    "wp": "干得漂亮",
    "gj": "干得好",
    "nt": "可惜了",
    "thx": "谢谢",
    "ty": "谢谢",
    "np": "不客气",
    "ez": "简单",
    "glhf": "祝好运",
    "brb": "马上回来",
    "afk": "暂离",
    "omw": "马上到",
    "lag": "网络卡",
    "dc": "掉线",
    "wp gg": "打得好",
    # -- ability / resource state -------------------------------------------
    "oom": "没蓝了",
    "cd": "冷却中",
    "ult": "大招",
    "no ult": "没大招",
    "mana": "蓝量",
    # -- generic actions ----------------------------------------------------
    "push": "推进",
    "retreat": "撤退",
    "dive": "突进",
    "poke": "消耗",
    "peel": "保护后排",
    "gank": "抓人",
    "roam": "游走",
    "rotate": "转点",
    # -- lanes / roles (MOBA) -----------------------------------------------
    "mid": "中路",
    "top": "上路",
    "bot": "下路",
    "jg": "打野",
    "jungle": "打野",
    "sup": "辅助",
    "support": "辅助",
    "carry": "核心",
    # -- FPS callouts -------------------------------------------------------
    "rush": "快攻",
    "peek": "探头对枪",
    "flank": "侧翼包抄",
    "eco": "经济局",
    "cover me": "掩护我",
    "hold": "守住",
    "on my mark": "听我口令",
    "one hp": "残血一个",
    "trading": "换人",
}


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Translator"
        # (renamed from FpsTranslator; the old folder is left alone so an
        #  upgraded install keeps its config, at the cost of one re-setup)
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "translator"


def default_config_path() -> Path:
    return app_data_dir() / "config.toml"


@dataclass
class ScreenConfig:
    engine: str = "auto"
    """auto | mss | dxgi

    ``auto`` means mss everywhere, including Windows: DXGI only yields a frame
    when the screen changes, so a static screen starves the pipeline. Choose
    ``dxgi`` explicitly if you want the cheaper path and accept that."""
    monitor: int = 1
    """1-based monitor index. Kept for compatibility, but do not trust it alone:
    mss counts a virtual 'all displays' entry first while Qt does not, so the
    two libraries can disagree about which screen is #1."""
    monitor_origin: tuple[int, int] | None = None
    """Absolute (x, y) top-left of the picked screen in the virtual desktop.

    This is what makes the picker and the capture agree. We match screens by
    position instead of by index, which is the only stable identifier across
    mss / Qt / DXGI enumeration orders."""
    region: tuple[float, float, float, float] | None = None
    """Single ROI as monitor fractions (x, y, w, h). Legacy; prefer ``regions``."""
    regions: list[tuple[float, float, float, float]] = field(default_factory=list)
    """One or more ROIs to scan, as monitor fractions.

    A game often puts chat in one corner and the kill feed in another, so
    several regions are normal. Each region costs its own OCR pass -- see the
    budget table in the README before adding many.
    Empty means "the whole monitor".
    """
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

    def resolved_regions(self) -> list[tuple[float, float, float, float] | None]:
        """The regions to scan, with legacy ``region`` folded in.

        ``[None]`` means the full monitor -- the source interprets None that way.
        """
        if self.regions:
            return list(self.regions)
        if self.region is not None:
            return [self.region]
        return [None]


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
    engine: str = "auto"
    """auto | llm | baidu | mock

    ``llm`` (any OpenAI-compatible endpoint) remains the default because it
    renders game slang far better. ``baidu`` is an alternative route: cheap,
    domestic and reliable, at the cost of conventional MT quality. ``auto``
    prefers llm, then baidu, then mock."""
    src_lang: str = "en"
    dst_lang: str = "zh"
    cache_size: int = 1024
    timeout_s: float = 20.0
    """Per-request deadline. Measured against the live DeepSeek API, time-to-first
    byte is ~0.2-0.5 s but total generation has ranged from 0.8 s to 28 s on the
    same prompt, so a tight deadline mostly produces fallbacks. 20 s keeps the
    common case working while still bounding a hung request."""
    retries: int = 1
    """Extra attempts after a timeout/5xx. Retried once because the slow tail is
    transient; more than that just doubles latency."""
    retry_backoff_s: float = 0.4
    glossary: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_GLOSSARY))
    """Setting [translate.glossary] REPLACES the built-in list."""
    glossary_extra: dict[str, str] = field(default_factory=dict)
    """[translate.glossary_extra] is merged ON TOP of ``glossary`` -- use this to
    add game-specific terms without losing the defaults."""
    context: str = ""
    """Free-form hint injected into the prompt, for per-game accuracy:

    "Overwatch. 'ult' = 终极技能. Keep hero names in English: Tracer, Winston."
    "Dota 2. 'tp' = 传送. 'deny' = 反补."
    """

    # --- engine = "baidu": fanyi-api.baidu.com --------------------------------
    baidu_appid: str = ""
    baidu_key: str = ""
    """Prefer the environment variables over pasting secrets into this file."""
    baidu_appid_env: str = "TRANSLATOR_BAIDU_APPID"
    baidu_key_env: str = "TRANSLATOR_BAIDU_KEY"
    baidu_glossary_prepass: bool = False
    """Substitute glossary terms in the *source* before sending to Baidu.

    Baidu has no prompt, so it renders "gank" as the transliteration "甘克" and
    "B site" as "B网站". Pre-substituting the known terms fixes those, but it
    also breaks the English sentence structure Baidu parses -- measured:
    "oom no mana" became the mixed "没蓝了 no 蓝量". Off by default; turn it on
    if your glossary is mostly short callouts rather than phrases.
    """
    baidu_qps: float = 1.0
    """Requests per second allowed by your Baidu plan. The free tier is 1; going
    over returns error 54003, so we rate-limit locally instead."""

    # --- engine = "llm": any OpenAI-compatible chat-completions endpoint -------
    base_url: str = "https://api.deepseek.com/v1"
    """Include the version prefix. Examples:
    https://api.deepseek.com/v1
    https://dashscope.aliyuncs.com/compatible-mode/v1
    https://api.moonshot.cn/v1
    http://localhost:11434/v1          (Ollama, no key needed)"""
    model: str = "deepseek-flash"
    """DeepSeek: deepseek-flash (fast, recommended) | deepseek-v4-pro (reasons,
    measured 10 s and an empty answer here -- not suitable for subtitles)."""
    api_key: str = ""
    """Prefer the environment variable over pasting a key into this file."""
    api_key_env: str = "TRANSLATOR_API_KEY"
    temperature: float = 0.0
    max_tokens: int = 2048
    """Ceiling, not a charge. Only generated tokens are billed."""
    disable_thinking: bool = True
    """Suppress reasoning on DeepSeek models. Reasoning adds latency and can
    consume the whole token budget, returning an empty answer."""
    extra_params: dict[str, object] = field(default_factory=dict)
    """Raw JSON merged into the request body, for provider-specific options
    (e.g. {"reasoning_effort": "none"}, {"top_p": 0.9})."""


@dataclass
class UIConfig:
    font_family: str = ""
    font_size: int = 20
    opacity: float = 0.88
    max_lines: int = 4
    max_width_ratio: float = 0.62
    """Widest a subtitle may get, as a fraction of the screen. Subtitles are
    always a single line; anything longer is elided rather than wrapped."""
    anchor: str = "roi-below"
    """Where the subtitle stack sits.

    ROI-relative (follows the region you selected with `translator roi`, so your eyes
    never leave the chat box):
        roi-below  just under the scanned region          (default)
        roi-above  just above it
        roi-over   centred on top of it, covering the original text
    Screen-relative (classic subtitle placement):
        bottom-center | top-center | bottom-left | bottom-right | center
    """
    roi_gap_px: int = 12
    """Gap between the scanned region and the subtitle stack."""
    keep_on_top: bool = True
    """Periodically re-assert WS_EX_TOPMOST (Windows only).

    A topmost window does not stay topmost forever: the game, Steam's overlay or
    Discord can raise themselves above us and our subtitles silently disappear
    behind them. On Windows we re-apply HWND_TOPMOST directly, which is free and
    does not remap the window. No-op elsewhere: Qt's raise_() on X11 pushes a
    topmost Tool window back into the normal layer, i.e. it does the opposite.
    """
    margin_px: int = 64
    show_source_text: bool = False
    show_latency: bool = False
    """Show the per-line latency badge. Debug aid; off by default."""
    line_ttl: float = 8.0
    """Seconds a subtitle stays on screen before fading out."""
    fade_in_ms: int = 220
    """Ramp-in time. A hard cut feels like a glitch mid-fight; a short fade reads
    as the game rendering it. 0 disables."""
    slide_in_px: int = 6
    """Subtitle drifts up by this many pixels while fading in."""
    outline: bool = True
    """Black outline around glyphs -- essential over bright gameplay."""
    background: bool = True
    background_opacity: float = 0.55
    """Dark panel behind each line, for readability over busy scenes."""
    screen_color: str = "#e8f0ff"
    voice_color: str = "#ffe9a8"


@dataclass
class FilterConfig:
    """What to throw away before spending a translation request on it."""

    min_chars: int = 2
    """Shorter than this is an OCR fragment, not a message."""
    min_letters: int = 1
    """At least this many alphabetic characters; drops '+150', '12', '3/4'."""
    drop_pure_symbols: bool = True
    drop_repeated_chars: bool = True
    max_repeat_ratio: float = 0.7
    """Drop when this share of the characters is one repeated symbol, which is
    what an OCR'd icon or health bar turns into."""

    confirm_frames: int = 1
    """How many consecutive OCR passes a line must appear at the same place.

    1 disables the check. 2 kills the two things a moving FPS camera puts inside
    the box: world text sliding past (camera + parallax) and transient damage
    numbers. Chat lines sit still for seconds, so they survive -- at the cost of
    one extra OCR interval of latency.
    """

    require_regex: list[str] = field(default_factory=list)
    """Keep ONLY lines matching one of these. Empty = keep everything.

    The strongest general lever: FPS chat is usually "Name: message", so
    ``require_regex = ["^\\S{1,24}\\s*[:：]\\s*\\S"]`` drops every HUD element that
    lacks a speaker prefix, without any per-game tuning.
    """

    extra_drop_regex: list[str] = field(default_factory=list)
    """Drop lines matching any of these. e.g. '^v?\\d+\\.\\d+' for version strings."""

    keep_regex: list[str] = field(default_factory=list)
    """Whitelist: a match here is never dropped, whatever else says otherwise."""


@dataclass
class PerformanceConfig:
    """The "do not hurt the game" knobs.

    A background overlay shares the machine with something that needs every
    core. These defaults favour low resource use over low latency; the budget
    they enforce is documented in the README and measured by ``translator bench``.
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
    filters: FilterConfig = field(default_factory=FilterConfig)

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
        if not isinstance(value, (list, tuple)):
            return value
        args = get_args(annotation)
        if origin is tuple and args and Ellipsis not in args:
            items = [
                _coerce(args[i] if i < len(args) else None, None, v) for i, v in enumerate(value)
            ]
        elif args and len(args) == 1:
            items = [_coerce(args[0], None, v) for v in value]
        else:
            items = list(value)
        return tuple(items) if origin is tuple else items
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


def _fmt_scalar(value: Any) -> str | None:
    """Format a TOML value, or None when it cannot be represented.

    TOML has no null literal. Writing ``str(None)`` produced ``region = "None"``,
    which then loaded back as the *string* "None" instead of the None our
    optional fields expect.
    """
    if value is None:
        return None
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
    scalar_items = {k: v for k, v in data.items() if not isinstance(v, dict) and v is not None}
    table_items = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, value in scalar_items.items():
        rendered = _fmt_scalar(value)
        if rendered is not None:
            lines.append(f"{_fmt_key(key)} = {rendered}")
    for section, table in table_items.items():
        lines.append("")
        lines.append(f"[{section}]")
        flat = {k: v for k, v in table.items() if not isinstance(v, dict) and v is not None}
        nested = {k: v for k, v in table.items() if isinstance(v, dict)}
        for key, value in flat.items():
            rendered = _fmt_scalar(value)
            if rendered is not None:
                lines.append(f"{_fmt_key(key)} = {rendered}")
        for sub, subtable in nested.items():
            lines.append("")
            lines.append(f"[{section}.{sub}]")
            for key, value in subtable.items():
                if value is None:
                    continue
                rendered = _fmt_scalar(value)
                if rendered is not None:
                    lines.append(f"{_fmt_key(key)} = {rendered}")
    return "\n".join(lines) + "\n"
