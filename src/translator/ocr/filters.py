"""Drop text that is not worth translating.

A game screen is full of text that is not a message: kill counters, ammo counts,
timers, ability cooldowns, player names boxed separately from what they said, and
whatever the OCR made of an icon. Translating those produces the "noise" the user
sees -- bubbles containing "+150", "3", "•••" or a bare nickname.

Filtering here rather than after translation has a second benefit: dropped lines
cost no API call at all.

The rules are deliberately conservative about *words*. Deciding that "Echo" is a
player name and "nice" is a word needs a dictionary we do not have, so bare names
are handled after translation (the model is asked to return "" for them, and a
line it returns unchanged is dropped). What this module removes is text that
cannot be a sentence at all.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..core.config import FilterConfig

__all__ = ["FilterConfig", "FilterChain", "reason_for"]

# Tokens that look like an identifier but are common game terms, never dropped as
# "looks like a name".
_GAME_TOKENS = {
    "gg",
    "wp",
    "gj",
    "nt",
    "ty",
    "thx",
    "ez",
    "np",
    "gl",
    "hf",
    "brb",
    "afk",
    "omw",
    "oom",
    "cd",
    "ult",
    "mid",
    "top",
    "bot",
    "jg",
    "sup",
    "adc",
    "tp",
    "hp",
    "mp",
    "dps",
    "tank",
    "heal",
    "push",
    "hold",
    "peek",
    "rush",
    "flank",
}

_REPEATED = re.compile(r"^(.)\1{2,}$")


@dataclass
class FilterChain:
    """Applies the configured rules and reports *why* a line was dropped."""

    config: FilterConfig = field(default_factory=FilterConfig)

    def __post_init__(self) -> None:
        self._extra = [re.compile(p, re.IGNORECASE) for p in self.config.extra_drop_regex]
        self._keep = [re.compile(p, re.IGNORECASE) for p in self.config.keep_regex]
        self._require = [re.compile(p, re.IGNORECASE) for p in self.config.require_regex]

    def reason_for(self, text: str) -> str | None:
        """Return a short reason when ``text`` should be skipped, else None."""
        return reason_for(text, self.config, self._extra, self._keep, self._require)


def _letters(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def reason_for(
    text: str,
    config: FilterConfig,
    extra: list[re.Pattern[str]] | None = None,
    keep: list[re.Pattern[str]] | None = None,
    require: list[re.Pattern[str]] | None = None,
) -> str | None:
    stripped = text.strip()
    if not stripped:
        return "empty"

    for pattern in keep or ():
        if pattern.search(stripped):
            return None
    for pattern in require or ():
        if pattern.search(stripped):
            break
    else:
        if require:
            return "no_require_match"
    for pattern in extra or ():
        if pattern.search(stripped):
            return "pattern"

    if len(stripped) < config.min_chars:
        return "too_short"

    if config.drop_pure_symbols:
        # Nothing letter-like and nothing numeric: pure decoration.
        if not any(ch.isalpha() or ch.isdigit() for ch in stripped):
            return "symbols"
        # Unicode category "So"/"Sk" only, e.g. "★ ★ ★".
        if all(unicodedata.category(ch).startswith("S") for ch in stripped):
            return "symbols"

    if _letters(stripped) < config.min_letters:
        return "no_letters"

    if config.drop_repeated_chars:
        if _REPEATED.match(stripped):
            return "repeated"
        if len(stripped) >= 4:
            top = max(stripped.count(ch) for ch in set(stripped))
            if top / len(stripped) >= config.max_repeat_ratio:
                return "repeated"

    if len(stripped) <= 20 and stripped.casefold() in _GAME_TOKENS:
        return None  # a known game term is a message even if it looks like an id

    return None
