from __future__ import annotations

import json

import httpx
import pytest

from fpst.core.config import TranslateConfig
from fpst.core.interfaces import Translator
from fpst.core.types import Lang
from fpst.translate import has_api_key, make_translator, resolve_engine
from fpst.translate.cache import CachingTranslator
from fpst.translate.llm import LLMTranslator


def _reply(content: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


def _engine(content: str, status: int = 200, **kwargs) -> LLMTranslator:
    defaults = {"api_key": "test-key", "transport": _reply(content, status)}
    defaults.update(kwargs)
    return LLMTranslator(**defaults)


# --------------------------------------------------------------- happy path


def test_batch_translation_json_array():
    engine = _engine('["快攻B点", "打得好"]')
    engine.warmup()
    out = engine.translate(["rush b", "gg wp"], Lang.EN, Lang.ZH)
    assert out == ["快攻B点", "打得好"]
    engine.close()


def test_markdown_fences_are_stripped():
    engine = _engine('```json\n["快攻", "掩护我"]\n```')
    engine.warmup()
    assert engine.translate(["rush", "cover me"], Lang.EN, Lang.ZH) == ["快攻", "掩护我"]
    engine.close()


def test_json_embedded_in_prose_is_recovered():
    engine = _engine('Sure! Here you go:\n["快攻", "守住"]\nHope that helps.')
    engine.warmup()
    assert engine.translate(["rush", "hold"], Lang.EN, Lang.ZH) == ["快攻", "守住"]
    engine.close()


def test_numbered_lines_fallback():
    engine = _engine("1. 快攻\n2. 守住")
    engine.warmup()
    assert engine.translate(["rush", "hold"], Lang.EN, Lang.ZH) == ["快攻", "守住"]
    engine.close()


def test_translations_key_wrapper():
    engine = _engine('{"translations": ["快攻", "守住"]}')
    engine.warmup()
    assert engine.translate(["rush", "hold"], Lang.EN, Lang.ZH) == ["快攻", "守住"]
    engine.close()


# ------------------------------------------------------------------ robustness


def test_short_answer_is_padded_with_source_text():
    """A model that returns 1 of 2 lines must not lose the second line."""
    engine = _engine('["快攻"]')
    engine.warmup()
    out = engine.translate(["rush", "hold the line"], Lang.EN, Lang.ZH)
    assert out == ["快攻", "hold the line"]
    engine.close()


def test_extra_answers_are_dropped():
    engine = _engine('["快攻", "守住", "多余"]')
    engine.warmup()
    assert engine.translate(["rush", "hold"], Lang.EN, Lang.ZH) == ["快攻", "守住"]
    engine.close()


def test_garbage_output_falls_back_to_source():
    engine = _engine("I cannot help with that.")
    engine.warmup()
    assert engine.translate(["rush", "hold"], Lang.EN, Lang.ZH) == ["rush", "hold"]
    assert engine.failures == 1
    engine.close()


def test_http_error_falls_back_to_source():
    engine = _engine("", status=500)
    engine.warmup()
    assert engine.translate(["rush"], Lang.EN, Lang.ZH) == ["rush"]
    assert engine.failures == 1
    engine.close()


def test_empty_input_makes_no_request():
    engine = _engine("[]")
    engine.warmup()
    assert engine.translate([], Lang.EN, Lang.ZH) == []
    assert engine.requests == 0
    engine.close()


def test_non_string_array_falls_back():
    engine = _engine("[1, 2]")
    engine.warmup()
    assert engine.translate(["rush", "hold"], Lang.EN, Lang.ZH) == ["rush", "hold"]
    engine.close()


# ---------------------------------------------------------------- request shape


def test_glossary_and_count_reach_the_prompt():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '["快攻"]'}}]})

    engine = LLMTranslator(
        api_key="k",
        transport=httpx.MockTransport(handler),
        glossary={"rush": "快攻"},
    )
    engine.warmup()
    engine.translate(["rush"], Lang.EN, Lang.ZH)
    engine.close()

    system = captured["messages"][0]["content"]  # type: ignore[index]
    assert "rush = 快攻" in system
    assert "exactly 1 entries" in system
    assert json.loads(captured["messages"][1]["content"]) == ["rush"]  # type: ignore[index]
    assert captured["temperature"] == 0.0


def test_missing_key_raises_a_helpful_error(monkeypatch):
    monkeypatch.delenv("FPST_API_KEY", raising=False)
    engine = LLMTranslator(api_key=None, api_key_env="FPST_API_KEY")
    with pytest.raises(RuntimeError, match="FPST_API_KEY"):
        engine.warmup()


# --------------------------------------------------------------- engine choice


def _cfg(**kwargs) -> TranslateConfig:
    cfg = TranslateConfig()
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def test_auto_without_key_falls_back_to_mock(monkeypatch):
    monkeypatch.delenv("FPST_API_KEY", raising=False)
    assert resolve_engine(_cfg(engine="auto")) == "mock"


def test_auto_with_env_key_uses_llm(monkeypatch):
    monkeypatch.setenv("FPST_API_KEY", "abc")
    assert has_api_key(_cfg()) is True
    assert resolve_engine(_cfg(engine="auto")) == "llm"


def test_auto_with_inline_key_uses_llm():
    assert resolve_engine(_cfg(engine="auto", api_key="abc")) == "llm"


def test_explicit_engine_is_respected_without_key(monkeypatch):
    monkeypatch.delenv("FPST_API_KEY", raising=False)
    assert resolve_engine(_cfg(engine="mock")) == "mock"


def test_make_translator_wraps_in_cache(monkeypatch):
    monkeypatch.delenv("FPST_API_KEY", raising=False)
    translator = make_translator(_cfg())
    assert isinstance(translator, CachingTranslator)
    assert isinstance(translator, Translator)


def test_cache_absorbs_repeated_lines():
    engine = _engine('["快攻"]')
    engine.warmup()
    cached = CachingTranslator(engine, maxsize=8)
    assert cached.translate(["rush"], Lang.EN, Lang.ZH) == ["快攻"]
    assert cached.translate(["rush"], Lang.EN, Lang.ZH) == ["快攻"]
    assert engine.requests == 1, "second call must come from the cache"
    assert cached.stats["hits"] == 1.0


def _capture_prompt(src: Lang, dst: Lang) -> str:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '["x"]'}}]})

    engine = LLMTranslator(api_key="k", transport=httpx.MockTransport(handler))
    engine.warmup()
    engine.translate(["rush"], src, dst)
    engine.close()
    return captured["messages"][0]["content"]  # type: ignore[index]


def test_prompt_states_the_target_language():
    """Regression: src/dst were accepted but never reached the prompt, so the
    model had to guess the output language."""
    prompt = _capture_prompt(Lang.EN, Lang.ZH)
    assert "into Simplified Chinese" in prompt
    assert "from English" in prompt


def test_changing_target_language_changes_the_prompt():
    assert "into Japanese" in _capture_prompt(Lang.EN, Lang.JA)


def test_prompt_contains_worked_examples():
    prompt = _capture_prompt(Lang.EN, Lang.ZH)
    assert "rush b fast" in prompt
    assert "快攻B点" in prompt
