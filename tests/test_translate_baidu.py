from __future__ import annotations

import hashlib

import httpx
import pytest

from translator.core.config import TranslateConfig
from translator.core.interfaces import Translator
from translator.core.types import Lang
from translator.translate import has_baidu, make_translator, resolve_engine
from translator.translate.baidu import ENDPOINT, ERROR_HINTS, BaiduTranslator
from translator.translate.cache import CachingTranslator


def _transport(payload: dict, captured: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
            captured["params"] = dict(request.url.params)
            captured["method"] = request.method
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _engine(payload: dict | None = None, captured: dict | None = None, **kwargs) -> BaiduTranslator:
    if payload is None:
        payload = {
            "from": "en",
            "to": "zh",
            "trans_result": [{"src": "gg", "dst": "打得好"}],
        }
    defaults = {
        "appid": "2024010100000001",
        "secret_key": "secret",
        "transport": _transport(payload, captured),
    }
    defaults.update(kwargs)
    return BaiduTranslator(**defaults)


# --------------------------------------------------------------------- happy


def test_single_line_translation():
    engine = _engine()
    engine.warmup()
    assert engine.translate(["gg"], Lang.EN, Lang.ZH) == ["打得好"]
    engine.close()


def test_multiple_lines_go_in_one_request():
    """Baidu splits q on newlines and answers line by line, so a screen update
    costs one request rather than one per chat line."""
    captured: dict = {}
    payload = {
        "trans_result": [
            {"src": "gg", "dst": "打得好"},
            {"src": "rush b", "dst": "快攻B点"},
        ]
    }
    engine = _engine(payload, captured)
    engine.warmup()
    out = engine.translate(["gg", "rush b"], Lang.EN, Lang.ZH)
    engine.close()

    assert out == ["打得好", "快攻B点"]
    assert captured["params"]["q"] == "gg\nrush b"
    assert engine.requests == 1


def test_request_matches_baidu_sample():
    """Sign, salt and parameter placement follow Baidu's own example: everything
    in the query string, sign = md5(appid + q + salt + key)."""
    captured: dict = {}
    engine = _engine(captured=captured, appid="APPID123", secret_key="KEY456")
    engine.warmup()
    engine.translate(["hello"], Lang.EN, Lang.ZH)
    engine.close()

    params = captured["params"]
    assert captured["method"] == "POST"
    assert ENDPOINT.endswith("/api/trans/vip/translate")
    assert params["appid"] == "APPID123"
    assert params["from"] == "en"
    assert params["to"] == "zh"
    assert params["q"] == "hello"
    assert "salt" in params and "sign" in params
    expected = hashlib.md5(("APPID123" + "hello" + params["salt"] + "KEY456").encode()).hexdigest()
    assert params["sign"] == expected


def test_salt_is_in_baidu_sample_range():
    captured: dict = {}
    engine = _engine(captured=captured)
    engine.warmup()
    engine.translate(["x"], Lang.EN, Lang.ZH)
    engine.close()
    assert 32768 <= int(captured["params"]["salt"]) <= 65536


# ------------------------------------------------------------------ alignment


def test_blank_lines_are_dropped_then_restored():
    """A blank entry in q would collapse Baidu's line alignment."""
    captured: dict = {}
    payload = {"trans_result": [{"src": "gg", "dst": "打得好"}]}
    engine = _engine(payload, captured)
    engine.warmup()
    out = engine.translate(["gg", "   ", ""], Lang.EN, Lang.ZH)
    engine.close()
    assert out == ["打得好", "", ""]
    assert captured["params"]["q"] == "gg"


def test_internal_newlines_are_flattened():
    """An embedded newline would look like an extra line to Baidu."""
    captured: dict = {}
    payload = {"trans_result": [{"src": "a b", "dst": "甲 乙"}]}
    engine = _engine(payload, captured)
    engine.warmup()
    out = engine.translate(["a\nb"], Lang.EN, Lang.ZH)
    engine.close()
    assert out == ["甲 乙"]
    assert "\n" not in captured["params"]["q"]


def test_long_batches_are_split_by_size():
    """Parameters travel in the query string, so a huge q would overflow the URL."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lines = request.url.params["q"].split("\n")
        calls.append(len(lines))
        return httpx.Response(
            200,
            json={"trans_result": [{"src": t, "dst": f"译{t}"} for t in lines]},
        )

    engine = BaiduTranslator(
        appid="a", secret_key="k", transport=httpx.MockTransport(handler), qps=1000
    )
    engine.warmup()
    texts = [f"line number {i} " + "x" * 50 for i in range(40)]
    out = engine.translate(texts, Lang.EN, Lang.ZH)
    engine.close()

    assert out == [f"译{t}" for t in texts]
    assert len(calls) > 1, "a 40-line batch must be chunked"
    assert sum(calls) == len(texts)


# -------------------------------------------------------------------- errors


def test_api_error_falls_back_to_source():
    engine = _engine({"error_code": "54001", "error_msg": "Invalid Sign"})
    engine.warmup()
    assert engine.translate(["gg"], Lang.EN, Lang.ZH) == ["gg"]
    assert engine.failures == 1
    engine.close()


def test_wrong_line_count_falls_back():
    engine = _engine({"trans_result": [{"src": "a", "dst": "甲"}]})
    engine.warmup()
    assert engine.translate(["a", "b"], Lang.EN, Lang.ZH) == ["a", "b"]
    engine.close()


def test_http_error_falls_back():
    engine = _engine({}, status=500)
    engine.warmup()
    assert engine.translate(["gg"], Lang.EN, Lang.ZH) == ["gg"]
    engine.close()


def test_missing_credentials_raise_a_helpful_error(monkeypatch):
    monkeypatch.delenv("TRANSLATOR_BAIDU_APPID", raising=False)
    monkeypatch.delenv("TRANSLATOR_BAIDU_KEY", raising=False)
    engine = BaiduTranslator(appid="", secret_key="")
    with pytest.raises(RuntimeError, match="appid"):
        engine.warmup()


def test_every_documented_error_code_has_a_hint():
    for code in ("52001", "52003", "54001", "54003", "54004", "58001"):
        assert code in ERROR_HINTS


def test_empty_input_makes_no_request():
    engine = _engine()
    engine.warmup()
    assert engine.translate([], Lang.EN, Lang.ZH) == []
    assert engine.requests == 0
    engine.close()


# ------------------------------------------------------------------- wiring


def _cfg(**kwargs) -> TranslateConfig:
    cfg = TranslateConfig()
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def test_llm_stays_the_default_engine():
    """Baidu is an additional route; the model translator remains the default
    because it renders game slang far better."""
    assert TranslateConfig().engine == "auto"
    assert resolve_engine(_cfg()) in {"llm", "mock"}


def test_baidu_is_selectable_explicitly():
    assert resolve_engine(_cfg(engine="baidu")) == "baidu"


def test_llm_wins_over_baidu_when_both_are_configured(monkeypatch):
    monkeypatch.setenv("TRANSLATOR_BAIDU_APPID", "id")
    monkeypatch.setenv("TRANSLATOR_BAIDU_KEY", "secret")
    monkeypatch.setenv("TRANSLATOR_API_KEY", "sk-x")
    assert resolve_engine(_cfg(engine="auto")) == "llm"


def test_baidu_is_used_when_only_it_is_configured(monkeypatch):
    monkeypatch.delenv("TRANSLATOR_API_KEY", raising=False)
    monkeypatch.setenv("TRANSLATOR_BAIDU_APPID", "id")
    monkeypatch.setenv("TRANSLATOR_BAIDU_KEY", "secret")
    assert resolve_engine(_cfg(engine="auto")) == "baidu"


def test_resolve_auto_prefers_baidu_when_configured(monkeypatch):
    monkeypatch.setenv("TRANSLATOR_BAIDU_APPID", "id")
    monkeypatch.setenv("TRANSLATOR_BAIDU_KEY", "secret")
    assert has_baidu(_cfg()) is True
    assert resolve_engine(_cfg(engine="auto")) == "baidu"


def test_make_translator_builds_baidu_behind_the_cache(monkeypatch):
    monkeypatch.setenv("TRANSLATOR_BAIDU_APPID", "id")
    monkeypatch.setenv("TRANSLATOR_BAIDU_KEY", "secret")
    translator = make_translator(_cfg(engine="baidu"))
    assert isinstance(translator, CachingTranslator)
    assert isinstance(translator, Translator)


def test_language_codes_match_baidu():
    from translator.translate.baidu import LANG_CODES

    assert LANG_CODES[Lang.JA] == "jp", "Baidu uses 'jp', not 'ja'"
    assert LANG_CODES[Lang.KO] == "kor"
    assert LANG_CODES[Lang.ZH] == "zh"
