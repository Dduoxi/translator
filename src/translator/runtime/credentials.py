"""API key handling: discovery, validation, and secure storage.

Release rule: the product is useless without a key, so the key is a hard
requirement at startup. Where the key lives matters though -- a plaintext
``api_key = "sk-..."`` in ``config.toml`` leaks the moment a user screenshots
their config or pastes it in a support thread.

Resolution order (first hit wins):

1. Environment variable -- highest priority, best for CI and power users
2. OS credential store -- Windows Credential Manager (DPAPI-encrypted, tied to
   the user account), macOS Keychain, Linux Secret Service
3. ``config.toml`` -- supported for compatibility, but we warn about it

Local endpoints (Ollama, llama.cpp, LM Studio) need no key at all, so the
requirement is skipped for loopback URLs.
"""

from __future__ import annotations

import getpass
import ipaddress
import logging
import os
import sys
from urllib.parse import urlparse

from ..core.config import TranslateConfig

log = logging.getLogger(__name__)

__all__ = [
    "SERVICE_NAME",
    "ensure_baidu",
    "ensure_credentials",
    "load_baidu",
    "save_baidu",
    "validate_baidu",
    "load_api_key",
    "save_api_key",
    "clear_api_key",
    "is_local_endpoint",
    "mask",
    "validate_api_key",
    "ensure_api_key",
    "MissingApiKey",
]

SERVICE_NAME = "translator"
LEGACY_SERVICE_NAME = "fps-translator"
"""Pre-rename service name. Read as a fallback so existing stored credentials
survive the rename, and rewritten on the next save."""
_ACCOUNT = "api_key"
_BAIDU_APPID = "baidu_appid"
_BAIDU_KEY = "baidu_key"


class MissingApiKey(RuntimeError):
    """No key could be found and we cannot ask for one."""


def can_prompt() -> bool:
    """True only when we have a terminal to type into.

    A packaged build is windowed: sys.stdin is None (or not a tty), so input()
    and getpass() cannot work. Calling them anyway raised and aborted startup
    before any window appeared -- "double-click, nothing happens".
    """
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - detached/closed stream
        return False


# --------------------------------------------------------------------- storage


def _keyring():
    try:
        import keyring

        # The "fail" backend silently swallows writes and returns None on read,
        # which would look like "no key stored". Treat it as unavailable.
        if type(keyring.get_keyring()).__module__.endswith(".fail"):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - optional dependency / no backend
        return None


def load_api_key(cfg: TranslateConfig) -> tuple[str, str]:
    """Return ``(key, source)``; source is ``env:<VAR>`` / ``keyring`` / ``config`` / ``none``."""
    from_env = os.environ.get(cfg.api_key_env, "").strip()
    if from_env:
        return from_env, f"env:{cfg.api_key_env}"

    store = _keyring()
    if store is not None:
        stored = None
        for service in (SERVICE_NAME, LEGACY_SERVICE_NAME):
            try:
                stored = store.get_password(service, _ACCOUNT)
            except Exception as exc:  # noqa: BLE001
                log.debug("credential store read failed: %s", exc)
                stored = None
            if stored:
                return stored.strip(), "keyring"

    if cfg.api_key.strip():
        return cfg.api_key.strip(), "config"

    return "", "none"


def save_api_key(key: str) -> str:
    """Persist the key; return the method used (``keyring`` or ``config``)."""
    key = key.strip()
    store = _keyring()
    if store is not None:
        try:
            store.set_password(SERVICE_NAME, _ACCOUNT, key)
            return "keyring"
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write to the credential store: %s", exc)
    return "config"


# ----------------------------------------------------------------- baidu keys


def load_baidu(cfg: TranslateConfig) -> tuple[str, str, str]:
    """Return ``(appid, secret, source)`` for Baidu Translate."""
    appid = os.environ.get(cfg.baidu_appid_env, "").strip() or cfg.baidu_appid.strip()
    secret = os.environ.get(cfg.baidu_key_env, "").strip() or cfg.baidu_key.strip()
    source = "env" if os.environ.get(cfg.baidu_appid_env, "").strip() else "config"

    if not (appid and secret):
        store = _keyring()
        if store is not None:
            stored_id = stored_key = None
            for service in (SERVICE_NAME, LEGACY_SERVICE_NAME):
                try:
                    stored_id = store.get_password(service, _BAIDU_APPID)
                    stored_key = store.get_password(service, _BAIDU_KEY)
                except Exception as exc:  # noqa: BLE001
                    log.debug("credential store read failed: %s", exc)
                    stored_id = stored_key = None
                if stored_id and stored_key:
                    break
            if stored_id and stored_key:
                return stored_id.strip(), stored_key.strip(), "keyring"
    if appid and secret:
        return appid, secret, source
    return "", "", "none"


def save_baidu(appid: str, secret: str) -> str:
    store = _keyring()
    if store is not None:
        try:
            store.set_password(SERVICE_NAME, _BAIDU_APPID, appid.strip())
            store.set_password(SERVICE_NAME, _BAIDU_KEY, secret.strip())
            return "keyring"
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write Baidu credentials to the keyring: %s", exc)
    return "config"


def validate_baidu(appid: str, secret: str, transport: object | None = None) -> tuple[bool, str]:
    """Translate one short word to prove the appid/secret pair is valid.

    Baidu signs every request, so a wrong secret fails immediately with 54001 --
    far better than discovering it at the first subtitle.
    """
    import hashlib
    import random

    import httpx

    from ..translate.baidu import ENDPOINT, ERROR_HINTS

    query = "hello"
    salt = str(random.randint(32768, 65536))
    sign = hashlib.md5((appid + query + salt + secret).encode()).hexdigest()
    import time

    payload: dict = {}
    response = None
    # The free tier allows 1 QPS, and configuring credentials usually happens
    # right after the user tried the service elsewhere, so a single 54003 here
    # would look like "your key is wrong". Retry once after a pause.
    for attempt in range(2):
        try:
            with httpx.Client(timeout=15.0, transport=transport) as client:  # type: ignore[arg-type]
                response = client.post(
                    ENDPOINT,
                    params={
                        "q": query,
                        "from": "en",
                        "to": "zh",
                        "appid": appid,
                        "salt": salt,
                        "sign": sign,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return False, f"连接失败：{exc}"

        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        payload = response.json()
        if str(payload.get("error_code", "")) in {"54003", "54005"} and attempt == 0:
            time.sleep(1.5)
            continue
        break
    if "error_code" in payload:
        code = str(payload["error_code"])
        return False, f"错误 {code}：{ERROR_HINTS.get(code, payload.get('error_msg', ''))}"
    results = payload.get("trans_result") or []
    if not results:
        return False, "返回为空，请检查 appid/密钥"
    return True, f"验证通过（hello → {results[0].get('dst', '')}）"


def ensure_baidu(
    cfg: TranslateConfig, *, interactive: bool = True, reason: str = ""
) -> tuple[str, str]:
    """Return a usable ``(appid, secret)``, prompting on first run."""
    appid, secret, source = load_baidu(cfg)
    if appid and secret:
        if source == "config":
            log.warning(
                "Baidu credentials read from config.toml (plaintext). "
                "Prefer `translator auth set` to store them in the OS credential store."
            )
        return appid, secret

    if not interactive or not can_prompt():
        raise MissingApiKey(
            f"未找到百度翻译凭据。请设置 {cfg.baidu_appid_env} / {cfg.baidu_key_env}，"
            "或在设置窗口里点「设置密钥…」。"
        )

    print()
    print("需要百度翻译 API 凭据（https://fanyi-api.baidu.com/manage/developer）")
    if reason:
        print(reason)
    try:
        typed_id = input("APPID: ").strip()
        typed_key = getpass.getpass("密钥（输入不回显）: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise MissingApiKey("已取消") from None
    if not typed_id or not typed_key:
        raise MissingApiKey("APPID 或密钥为空")

    ok, message = validate_baidu(typed_id, typed_key)
    if not ok:
        raise MissingApiKey(f"凭据校验失败：{message}")

    method = save_baidu(typed_id, typed_key)
    if method == "keyring":
        print(f"已保存到系统凭据管理器（APPID {typed_id}）")
    else:
        print("系统凭据管理器不可用，凭据将写入 config.toml。")
        cfg.baidu_appid = typed_id
        cfg.baidu_key = typed_key
    return typed_id, typed_key


def ensure_credentials(cfg: TranslateConfig, *, interactive: bool = True, reason: str = "") -> str:
    """Ask for whatever the configured engine needs. Returns the engine used."""
    from ..translate import resolve_engine

    engine = resolve_engine(cfg)
    if engine == "baidu":
        ensure_baidu(cfg, interactive=interactive, reason=reason)
    elif engine in ("llm", "openai", "deepseek", "ollama", "kimi", "qwen"):
        ensure_api_key(cfg, interactive=interactive, reason=reason)
    return engine


def clear_api_key() -> bool:
    """Remove the stored key. Returns True if something was deleted."""
    store = _keyring()
    if store is None:
        return False
    try:
        store.delete_password(SERVICE_NAME, _ACCOUNT)
        return True
    except Exception:  # noqa: BLE001 - not found / no backend
        return False


def mask(key: str) -> str:
    """``sk-d06e...6fb99`` -- enough to identify a key, useless to an attacker."""
    if not key:
        return "(none)"
    if len(key) <= 12:
        return key[:2] + "***"
    return f"{key[:6]}...{key[-4:]}"


# ------------------------------------------------------------------ validation


def is_local_endpoint(base_url: str) -> bool:
    """True for loopback hosts, where no key is expected (Ollama, LM Studio)."""
    try:
        host = (urlparse(base_url).hostname or "").strip().lower()
    except ValueError:
        return False
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_api_key(
    key: str,
    base_url: str,
    model: str,
    timeout_s: float = 20.0,
    transport: object | None = None,
) -> tuple[bool, str]:
    """Make one tiny real request. Cheap, and catches typos immediately.

    Returns ``(ok, message)`` where message is user-facing.
    """
    import httpx

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "max_tokens": 16,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": "ping"}],
    }
    if model.startswith("deepseek"):
        payload["thinking"] = {"type": "disabled"}
    try:
        with httpx.Client(timeout=timeout_s, transport=transport) as client:  # type: ignore[arg-type]
            response = client.post(url, json=payload, headers={"Authorization": f"Bearer {key}"})
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{exc}"

    if response.status_code == 200:
        return True, "验证通过"
    if response.status_code in (401, 403):
        return False, "密钥无效或已失效（401/403）"
    if response.status_code == 404:
        return False, f"模型或路径不存在（404）：检查 base_url 和 model（{model}）"
    if response.status_code == 402:
        return False, "余额不足（402）"
    body = response.text[:160].replace("\n", " ")
    return False, f"HTTP {response.status_code}: {body}"


# ------------------------------------------------------------------------ gate


_PROMPT = """
需要翻译密钥（{base_url}）
模型：{model}
没有密钥无法翻译。

{reason}
请粘贴 API Key（输入不回显）：
"""


def ensure_api_key(
    cfg: TranslateConfig,
    *,
    interactive: bool = True,
    reason: str = "",
) -> str:
    """Return a usable key, prompting on first run. Raises if none is usable.

    An empty string means "this endpoint needs no key" (local model).
    """
    if is_local_endpoint(cfg.base_url):
        return ""

    key, source = load_api_key(cfg)
    if key:
        if source == "config":
            log.warning(
                "API key read from config.toml (plaintext). Consider storing it in "
                "the OS credential store instead: run `translator auth set`."
            )
        return key

    if not interactive:
        raise MissingApiKey(
            f"no API key found. Set {cfg.api_key_env}, or run `translator auth set`."
        )

    print(_PROMPT.format(base_url=cfg.base_url, model=cfg.model, reason=reason))
    try:
        typed = getpass.getpass("> ").strip()
    except (EOFError, KeyboardInterrupt):
        raise MissingApiKey("已取消") from None

    if not typed:
        raise MissingApiKey("未输入密钥")

    ok, message = validate_api_key(typed, cfg.base_url, cfg.model, cfg.timeout_s)
    if not ok:
        raise MissingApiKey(f"密钥校验失败：{message}")

    method = save_api_key(typed)
    if method == "keyring":
        print(f"已保存到系统凭据管理器（{mask(typed)}）")
    else:
        print(f"系统凭据管理器不可用，密钥将写入 config.toml（{mask(typed)})。")
        cfg.api_key = typed
    return typed
