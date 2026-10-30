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
    assert "exactly 1 strings" in system
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


def test_empty_string_means_skip_and_is_preserved():
    """The prompt asks for "" on noise lines; the pipeline drops them."""
    engine = _engine('["打得好", "", ""]')
    engine.warmup()
    out = engine.translate(["gg", "+150", "PlayerOne"], Lang.EN, Lang.ZH)
    assert out == ["打得好", "", ""]
    engine.close()


def test_empty_on_count_mismatch_still_falls_back_to_source():
    """If the model also got the count wrong, alignment is untrustworthy, so
    every empty degrades to the source line rather than being skipped."""
    engine = _engine('["", ""]')
    engine.warmup()
    out = engine.translate(["gg", "+150", "PlayerOne"], Lang.EN, Lang.ZH)
    assert out == ["gg", "+150", "PlayerOne"]
    engine.close()


def test_glossary_is_filtered_to_terms_in_the_batch():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '["x"]'}}]})

    engine = LLMTranslator(
        api_key="k",
        transport=httpx.MockTransport(handler),
        glossary={"rush": "快攻", "flank": "侧翼包抄", "eco": "经济局"},
    )
    engine.warmup()
    engine.translate(["rush b fast"], Lang.EN, Lang.ZH)
    engine.close()

    system = captured["messages"][0]["content"]  # type: ignore[index]
    assert "rush = 快攻" in system
    assert "flank" not in system  # not in this batch
    assert "eco" not in system


def test_unmatched_glossary_is_not_injected_at_all():
    """Injecting 40+ irrelevant terms would waste tokens and add noise."""
    prompt = _prompt_for(["hello there"], {"rush": "快攻"})
    assert "rush = 快攻" not in prompt
    assert "no glossary term appears" in prompt


def test_token_budget_scales_with_batch_and_is_capped():
    engine = LLMTranslator(api_key="k", api_key_env="X", max_tokens=4096)
    short = engine._token_budget(["ab"])
    long = engine._token_budget(["x" * 2000])
    assert short >= 256
    assert long > short
    assert long <= 4096


# ------------------------------------------------------- glossary word bounds


def _prompt_for(texts, glossary, context=""):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    engine = LLMTranslator(
        api_key="k",
        transport=httpx.MockTransport(handler),
        glossary=glossary,
        context=context,
    )
    engine.warmup()
    engine.translate(texts, Lang.EN, Lang.ZH)
    engine.close()
    return captured["messages"][0]["content"]  # type: ignore[index]


def test_short_abbreviation_does_not_match_inside_a_word():
    """Regression: 'gg' is a substring of 'aggressive'."""
    prompt = _prompt_for(["he is aggressive"], {"gg": "打得好"})
    assert "gg = 打得好" not in prompt


def test_abbreviation_matches_as_a_whole_word():
    prompt = _prompt_for(["gg wp team"], {"gg": "打得好"})
    assert "gg = 打得好" in prompt


def test_abbreviation_matches_with_punctuation_around_it():
    prompt = _prompt_for(["nice, gg!"], {"gg": "打得好"})
    assert "gg = 打得好" in prompt


def test_multi_word_glossary_term_matches():
    prompt = _prompt_for(["please cover me now"], {"cover me": "掩护我"})
    assert "cover me = 掩护我" in prompt


# ------------------------------------------------------------ genre coverage


def test_prompt_is_not_fps_specific():
    prompt = _prompt_for(["gank mid"], {})
    assert "MOBA" in prompt
    assert "hero shooter" in prompt
    assert "first-person shooter" not in prompt


def test_prompt_explains_abbreviations_generically():
    prompt = _prompt_for(["oom"], {})
    assert "oom" in prompt and "gank" in prompt and "ult" in prompt


def test_context_is_injected():
    prompt = _prompt_for(["ult ready"], {}, context="Overwatch. ult = 终极技能.")
    assert "Overwatch. ult = 终极技能." in prompt


def test_empty_context_shows_placeholder():
    assert "(none)" in _prompt_for(["hi"], {})


# ------------------------------------------------------- glossary_extra merge


def test_glossary_extra_merges_over_defaults():
    from fpst.core.config import DEFAULT_GLOSSARY
    from fpst.translate import merged_glossary

    cfg = TranslateConfig(glossary=dict(DEFAULT_GLOSSARY))
    cfg.glossary_extra = {"tp": "传送", "gg": "牛"}
    merged = merged_glossary(cfg)
    assert merged["tp"] == "传送"
    assert merged["gg"] == "牛"  # override wins
    assert merged["wp"] == "干得漂亮"  # default survives


def test_setting_glossary_replaces_the_defaults():
    from fpst.translate import merged_glossary

    cfg = TranslateConfig(glossary={"tp": "传送"})
    assert merged_glossary(cfg) == {"tp": "传送"}


def test_blank_entries_are_dropped():
    from fpst.translate import merged_glossary

    cfg = TranslateConfig(glossary={"a": "b", "c": "  "})
    assert merged_glossary(cfg) == {"a": "b"}


# --------------------------------------------------------- reasoning suppression


def _payload_for(**kwargs):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '["x"]'}}]})

    defaults = {"api_key": "k", "transport": httpx.MockTransport(handler)}
    defaults.update(kwargs)
    engine = LLMTranslator(**defaults)
    engine.warmup()
    engine.translate(["rush"], Lang.EN, Lang.ZH)
    engine.close()
    return captured


def test_deepseek_requests_disable_thinking():
    """DeepSeek V4 reasons by default; that produced empty answers and +0.6 s."""
    payload = _payload_for(model="deepseek-flash")
    assert payload["thinking"] == {"type": "disabled"}


def test_non_deepseek_models_do_not_get_the_param():
    """Sending an unknown field to OpenAI/Ollama can 400 the request."""
    payload = _payload_for(model="gpt-4o-mini")
    assert "thinking" not in payload


def test_thinking_can_be_left_enabled_for_reasoning_models():
    payload = _payload_for(model="deepseek-flash", disable_thinking=False)
    assert "thinking" not in payload


def test_extra_params_are_merged_into_the_request():
    payload = _payload_for(model="gpt-4o-mini", extra_params={"top_p": 0.9})
    assert payload["top_p"] == 0.9


def test_extra_params_can_override_the_thinking_default():
    payload = _payload_for(model="deepseek-flash", extra_params={"thinking": {"type": "enabled"}})
    assert payload["thinking"] == {"type": "enabled"}


def test_token_floor_is_512_not_256():
    engine = LLMTranslator(api_key="k", max_tokens=2048)
    assert engine._token_budget(["ab"]) == 512
    assert engine._token_budget(["x" * 3000]) == 2048
