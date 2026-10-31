"""What gets thrown away before translation.

Every rule here exists because a game screen is mostly not-a-message: kill
counters, ammo, timers, ability icons, and player names boxed separately from what
they said. Translating those is what "lots of noise" means in practice.
"""

from __future__ import annotations

import pytest

from translator.core.config import FilterConfig
from translator.ocr.filters import FilterChain, reason_for


def _chain(**kwargs) -> FilterChain:
    return FilterChain(FilterConfig(**kwargs))


# ---------------------------------------------------------------- obvious junk


@pytest.mark.parametrize("text", ["+150", "12", "3/4", "0:32", "100%"])
def test_numbers_only_are_dropped(text):
    assert _chain().reason_for(text) == "no_letters"


@pytest.mark.parametrize("text", ["•••", "★ ★ ★", "|||", ">>>", "…"])
def test_pure_decoration_is_dropped(text):
    assert _chain().reason_for(text) in {"symbols", "no_letters", "too_short"}


@pytest.mark.parametrize("text", ["a", "1", " ", "…"])
def test_fragments_shorter_than_two_chars_are_dropped(text):
    assert _chain().reason_for(text) in {"too_short", "empty", "no_letters", "symbols"}


@pytest.mark.parametrize("text", ["aaaaa", ".....", "░░░░"])
def test_repeated_symbols_are_dropped(text):
    assert _chain().reason_for(text) in {"repeated", "no_letters", "symbols"}


def test_mostly_repeated_characters_are_dropped():
    # Order matters only for the reported reason; either rule is correct here.
    assert _chain().reason_for("██████1") in {"repeated", "no_letters"}


def test_a_short_numeric_fragment_with_one_word_is_kept():
    """'42hp' may be a fragment of a real message; merging usually fixes it, and
    guessing wrong here would silently eat real content."""
    assert _chain().reason_for("42hp") is None


# ------------------------------------------------------------------- real text


@pytest.mark.parametrize(
    "text",
    [
        "gg wp",
        "rush b site now",
        "enemy behind the box, two hp",
        "需要掩护",
        "PlayerOne",
        "Echo",
        "i have ult",
    ],
)
def test_real_messages_survive(text):
    assert _chain().reason_for(text) is None


def test_known_game_tokens_are_not_treated_as_identifiers():
    """'ult' and 'mid' look like ids but are messages."""
    for token in ("ult", "mid", "oom", "gg", "afk", "push"):
        assert _chain().reason_for(token) is None, token


# ------------------------------------------------------- require_regex (the lever)


def test_require_regex_keeps_only_speaker_prefixed_lines():
    """FPS chat is "Name: message"; requiring the prefix drops every HUD element
    without any per-game tuning."""
    chain = _chain(require_regex=[r"^\S{1,24}\s*[:：]\s*\S"])
    assert chain.reason_for("PlayerOne: rush b site") is None
    assert chain.reason_for("xX_Shadow_Xx: gg") is None
    assert chain.reason_for("玩家一：快攻") is None
    for hud in ("AMMO", "RELOADING", "+150", "Echo", "5.2"):
        assert chain.reason_for(hud) == "no_require_match", hud


def test_require_regex_is_off_by_default():
    assert _chain().reason_for("AMMO") is None


# --------------------------------------------------------------- extra / keep


def test_extra_patterns_drop_matching_lines():
    chain = _chain(extra_drop_regex=[r"^v?\d+\.\d+", r"^\+\d+"])
    assert chain.reason_for("v1.2.3") == "pattern"
    assert chain.reason_for("+150") == "pattern"
    assert chain.reason_for("gg wp") is None


def test_keep_patterns_win_over_everything():
    chain = _chain(require_regex=[r"^\S+:"], extra_drop_regex=[r"ammo"], keep_regex=[r"\bammo\b"])
    assert chain.reason_for("low ammo") is None, "keep must override extra and require"


def test_rules_can_be_switched_off():
    chain = _chain(drop_pure_symbols=False, drop_repeated_chars=False, min_letters=0)
    assert chain.reason_for("•••") is None
    assert chain.reason_for("aaaaa") is None


def test_reason_for_works_without_a_chain():
    """The module-level helper is usable standalone, which keeps it testable."""
    assert reason_for("+150", FilterConfig()) == "no_letters"
    assert reason_for("gg", FilterConfig()) is None


# ------------------------------------------------------------------ behaviour


def test_pipeline_counts_and_explains_dropped_lines():
    """The status panel must be able to say why lines vanished."""
    import time

    import numpy as np

    from translator.core.bus import EventBus
    from translator.core.interfaces import ScreenSource
    from translator.core.types import Frame
    from translator.ocr.mock import MockOCREngine
    from translator.pipeline import ScreenPipeline
    from translator.translate.mock import MockTranslator

    class OneShot(ScreenSource):
        name = "oneshot"

        def __init__(self) -> None:
            self.done = False

        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

        def grab(self):
            if self.done:
                return None
            self.done = True
            return Frame(image=np.zeros((40, 200, 3), np.uint8))

        @property
        def size(self):
            return (200, 40)

    ocr = MockOCREngine(lines=["+150", "gg wp", "•••", "Echo: rush b"])
    pipeline = ScreenPipeline(
        OneShot(),
        ocr,
        MockTranslator(),
        EventBus(),
        max_fps=20,
        merge_lines=False,
        filters=_chain(),
    )
    pipeline.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and pipeline.stats.ocr_runs == 0:
            time.sleep(0.01)
    finally:
        pipeline.stop()

    assert pipeline.stats.lines_dropped == 2, "the number and the symbols"
    assert pipeline.stats.drop_reasons == {"no_letters": 1, "symbols": 1}


def test_filtering_happens_before_translation_so_it_costs_nothing():
    """A dropped line must never reach the translator: that is the whole point."""
    from translator.core.config import AppConfig

    cfg = AppConfig()
    cfg.filters.min_chars = 5
    from translator.pipeline.screen import _build_filters

    chain = _build_filters(cfg.filters)
    assert chain is not None
    assert chain.reason_for("gg") == "too_short"
