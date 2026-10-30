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
from urllib.parse import urlparse

from ..core.config import TranslateConfig

log = logging.getLogger(__name__)

__all__ = [
    "SERVICE_NAME",
    "load_api_key",
    "save_api_key",
    "clear_api_key",
    "is_local_endpoint",
    "mask",
    "validate_api_key",
    "ensure_api_key",
    "MissingApiKey",
]

SERVICE_NAME = "fps-translator"
_ACCOUNT = "api_key"


class MissingApiKey(RuntimeError):
    """No key could be found and we cannot ask for one."""


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
        try:
            stored = store.get_password(SERVICE_NAME, _ACCOUNT)
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
                "the OS credential store instead: run `fpst auth set`."
            )
        return key

    if not interactive:
        raise MissingApiKey(f"no API key found. Set {cfg.api_key_env}, or run `fpst auth set`.")

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
