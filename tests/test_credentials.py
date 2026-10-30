from __future__ import annotations

import httpx
import pytest

from fpst.core.config import TranslateConfig
from fpst.runtime import credentials as creds
from fpst.runtime.credentials import (
    MissingApiKey,
    clear_api_key,
    ensure_api_key,
    is_local_endpoint,
    load_api_key,
    mask,
    save_api_key,
    validate_api_key,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail_writes = False

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail_writes:
            raise RuntimeError("no backend")
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        # Real keyring raises when the entry does not exist; mimic that so
        # clear_api_key()'s return value is exercised honestly.
        if (service, account) not in self.store:
            raise KeyError("not found")
        del self.store[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch):
    ring = FakeKeyring()
    monkeypatch.setattr(creds, "_keyring", lambda: ring)
    monkeypatch.delenv("FPST_API_KEY", raising=False)
    return ring


def _cfg(**kwargs) -> TranslateConfig:
    cfg = TranslateConfig()
    cfg.api_key = ""
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


# ------------------------------------------------------------------- storing


def test_save_and_load_roundtrip(fake_keyring):
    assert save_api_key("sk-test-123") == "keyring"
    key, source = load_api_key(_cfg())
    assert key == "sk-test-123"
    assert source == "keyring"


def test_env_var_beats_keyring(fake_keyring, monkeypatch):
    save_api_key("from-keyring")
    monkeypatch.setenv("FPST_API_KEY", "from-env")
    key, source = load_api_key(_cfg())
    assert key == "from-env"
    assert source == "env:FPST_API_KEY"


def test_keyring_beats_plaintext_config(fake_keyring):
    save_api_key("from-keyring")
    key, source = load_api_key(_cfg(api_key="from-config"))
    assert key == "from-keyring"
    assert source == "keyring"


def test_config_is_the_last_resort(monkeypatch):
    monkeypatch.setattr(creds, "_keyring", lambda: None)
    monkeypatch.delenv("FPST_API_KEY", raising=False)
    key, source = load_api_key(_cfg(api_key="from-config"))
    assert key == "from-config"
    assert source == "config"


def test_nothing_stored(monkeypatch):
    monkeypatch.setattr(creds, "_keyring", lambda: None)
    monkeypatch.delenv("FPST_API_KEY", raising=False)
    assert load_api_key(_cfg()) == ("", "none")


def test_save_falls_back_to_config_when_the_store_fails(fake_keyring):
    fake_keyring.fail_writes = True
    assert save_api_key("sk-x") == "config"


def test_clear_removes_the_key(fake_keyring):
    save_api_key("sk-test")
    assert clear_api_key() is True
    assert load_api_key(_cfg())[0] == ""


def test_clear_on_empty_store_is_harmless(fake_keyring):
    assert clear_api_key() is False


# ------------------------------------------------------------------ masking


def test_mask_hides_the_middle():
    assert mask("sk-d06eeb9fe4f64073a334905a2376fb99") == "sk-d06...fb99"


def test_mask_short_key():
    assert mask("sk-123") == "sk***"


def test_mask_empty():
    assert mask("") == "(none)"


# ----------------------------------------------------------------- endpoints


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:1234/v1",
        "http://[::1]:8080/v1",
        "http://LOCALHOST/v1",
    ],
)
def test_loopback_needs_no_key(url):
    assert is_local_endpoint(url) is True


@pytest.mark.parametrize(
    "url",
    ["https://api.deepseek.com/v1", "https://api.moonshot.cn/v1", "http://10.0.0.5/v1"],
)
def test_remote_needs_a_key(url):
    assert is_local_endpoint(url) is False


# ---------------------------------------------------------------- validation


def _transport(status: int, body: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(status, json=body or {"choices": []})

    return httpx.MockTransport(handler)


def test_validate_accepts_200():
    ok, message = validate_api_key(
        "sk-x", "https://api.deepseek.com/v1", "deepseek-flash", transport=_transport(200)
    )
    assert ok is True


def test_validate_reports_a_bad_key():
    ok, message = validate_api_key(
        "sk-bad", "https://api.deepseek.com/v1", "m", transport=_transport(401)
    )
    assert ok is False
    assert "401" in message or "无效" in message


def test_validate_reports_missing_model():
    ok, message = validate_api_key(
        "sk-x", "https://api.deepseek.com/v1", "nope", transport=_transport(404)
    )
    assert ok is False
    assert "404" in message


def test_validate_reports_no_balance():
    ok, message = validate_api_key(
        "sk-x", "https://api.deepseek.com/v1", "m", transport=_transport(402)
    )
    assert ok is False
    assert "402" in message


def test_validate_reports_network_failure():
    def boom(request):
        raise httpx.ConnectError("dns")

    ok, message = validate_api_key(
        "sk-x", "https://api.deepseek.com/v1", "m", transport=httpx.MockTransport(boom)
    )
    assert ok is False
    assert "连接失败" in message


# ---------------------------------------------------------------------- gate


def test_local_endpoint_bypasses_the_requirement(monkeypatch):
    monkeypatch.setattr(creds, "_keyring", lambda: None)
    cfg = _cfg(base_url="http://localhost:11434/v1")
    assert ensure_api_key(cfg, interactive=False) == ""


def test_missing_key_without_a_prompt_raises(fake_keyring):
    with pytest.raises(MissingApiKey, match="FPST_API_KEY"):
        ensure_api_key(_cfg(), interactive=False)


def test_existing_key_is_used_without_prompting(fake_keyring):
    save_api_key("sk-stored")
    assert ensure_api_key(_cfg(), interactive=False) == "sk-stored"


def test_prompt_saves_a_key_that_validates(fake_keyring, monkeypatch, capsys):
    monkeypatch.setattr(creds.getpass, "getpass", lambda prompt="": "sk-good")
    monkeypatch.setattr(
        creds,
        "validate_api_key",
        lambda *a, **k: (True, "验证通过"),
    )
    assert ensure_api_key(_cfg(), interactive=True) == "sk-good"
    assert load_api_key(_cfg()) == ("sk-good", "keyring")


def test_prompt_rejects_a_key_that_fails_validation(fake_keyring, monkeypatch):
    monkeypatch.setattr(creds.getpass, "getpass", lambda prompt="": "sk-bad")
    monkeypatch.setattr(
        creds, "validate_api_key", lambda *a, **k: (False, "密钥无效或已失效（401/403）")
    )
    with pytest.raises(MissingApiKey, match="校验失败"):
        ensure_api_key(_cfg(), interactive=True)
    # Nothing must be persisted on failure.
    assert load_api_key(_cfg())[0] == ""


def test_prompt_with_empty_input_raises(fake_keyring, monkeypatch):
    monkeypatch.setattr(creds.getpass, "getpass", lambda prompt="": "   ")
    with pytest.raises(MissingApiKey, match="未输入"):
        ensure_api_key(_cfg(), interactive=True)


def test_ctrl_c_during_prompt_is_not_a_crash(fake_keyring, monkeypatch):
    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(creds.getpass, "getpass", interrupt)
    with pytest.raises(MissingApiKey, match="取消"):
        ensure_api_key(_cfg(), interactive=True)
