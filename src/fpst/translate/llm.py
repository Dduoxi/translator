"""OpenAI-compatible chat-completions translator.

DeepSeek, 通义/DashScope, Kimi/Moonshot and local Ollama all speak the same
``POST {base_url}/chat/completions`` protocol, so one implementation covers
every provider -- only ``base_url``/``model`` change.

Two things make this different from a plain MT API:

* **Quality on game slang.** General MT renders "rush" as "冲" and "eco" as
  "生态". An instruction-following model gets the tactical meaning right, and we
  can hand it a glossary in the prompt.
* **Batching is mandatory, not an optimisation.** Every call costs a round trip
  plus tokens, so four chat lines must be one request, not four.

Failure policy: if the model misbehaves (bad JSON, wrong count, timeout) we
return the *source* text rather than dropping the line -- seeing English beats
seeing nothing, and the overlay stays consistent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence

from ..core.interfaces import Translator
from ..core.registry import register
from ..core.types import Lang

log = logging.getLogger(__name__)

__all__ = ["LLMTranslator"]

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_ARRAY = re.compile(r"\[.*\]", re.DOTALL)
_NUMBERED = re.compile(r"^\s*\d+[.)]\s*")

SYSTEM_PROMPT = """You translate live in-game chat for a first-person shooter.

Rules:
- Output ONLY the translation. No explanations, no notes, no quotes.
- Keep player names, tags, numbers and codes exactly as written (PlayerOne, 2hp, B site).
- Keep the terse, spoken style of in-game chat. Never make it formal or literary.
- Prefer the tactical meaning over the literal one.
- If a line is only a player name, an emote or untranslatable, return it unchanged.

Glossary (follow it when a term appears):
{glossary}

Output format: a single JSON array of strings, with exactly {count} entries, one
per input line in the same order. No markdown fences, no other text."""


class LLMTranslator(Translator):
    name = "llm"

    def __init__(
        self,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        api_key: str | None = None,
        api_key_env: str = "FPST_API_KEY",
        glossary: dict[str, str] | None = None,
        timeout_s: float = 6.0,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        transport: object | None = None,
        **_ignored: object,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key or os.environ.get(api_key_env, "").strip()
        self._api_key_env = api_key_env
        self._glossary = {k: v for k, v in (glossary or {}).items() if k}
        self._timeout = timeout_s
        self._temperature = temperature
        self._max_tokens = max_tokens
        # Injected in tests so no socket is ever opened.
        self._transport = transport
        self._client = None
        self.requests = 0
        self.failures = 0

    # ------------------------------------------------------------- lifecycle
    def warmup(self) -> None:
        if self._client is not None:
            return
        if not self._api_key:
            raise RuntimeError(
                f"no API key: set {self._api_key_env} (or [translate] api_key). "
                f"Engine 'llm' needs a key for {self._base_url}"
            )
        import httpx

        self._client = httpx.Client(
            timeout=self._timeout,
            transport=self._transport,  # type: ignore[arg-type]
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None

    # ------------------------------------------------------------- translate
    def translate(self, texts: Sequence[str], src: Lang, dst: Lang) -> list[str]:
        if not texts:
            return []
        if self._client is None:
            self.warmup()

        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        glossary=self._glossary_text(), count=len(texts)
                    ),
                },
                {"role": "user", "content": json.dumps(list(texts), ensure_ascii=False)},
            ],
        }

        self.requests += 1
        try:
            assert self._client is not None
            response = self._client.post(f"{self._base_url}/chat/completions", json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - never take the pipeline down
            self.failures += 1
            log.warning("translation request failed (%s); falling back to source text", exc)
            return list(texts)

        parsed = self._parse(content, list(texts))
        if parsed is None:
            self.failures += 1
            log.warning("could not parse model output: %r", content[:200])
            return list(texts)
        return parsed

    # ------------------------------------------------------------------ parse
    def _parse(self, content: str, fallback: list[str]) -> list[str] | None:
        """Best-effort recovery of a list of strings from model output."""
        cleaned = _FENCE.sub("", content.strip())

        candidates: list[str] = [cleaned]
        match = _ARRAY.search(cleaned)
        if match and match.group(0) != cleaned:
            candidates.append(match.group(0))

        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list) and all(isinstance(item, str) for item in data):
                return self._fit(data, fallback)
            if isinstance(data, dict) and isinstance(data.get("translations"), list):
                items = data["translations"]
                if all(isinstance(item, str) for item in items):
                    return self._fit(items, fallback)

        # Last resort: one line per input, stripping "1. " style prefixes.
        lines = [line for line in cleaned.splitlines() if line.strip()]
        if len(lines) == len(fallback):
            return [_NUMBERED.sub("", line).strip() for line in lines]
        return None

    @staticmethod
    def _fit(items: list[str], fallback: list[str]) -> list[str]:
        """Align the model's list to our input: pad with source, drop extras."""
        if len(items) == len(fallback):
            return [item.strip() or source for item, source in zip(items, fallback, strict=True)]
        log.debug("model returned %d items for %d inputs", len(items), len(fallback))
        out = []
        for i, source in enumerate(fallback):
            value = items[i].strip() if i < len(items) else ""
            out.append(value or source)
        return out

    def _glossary_text(self) -> str:
        if not self._glossary:
            return "(none)"
        return ", ".join(f"{k} = {v}" for k, v in self._glossary.items())

    @property
    def stats(self) -> dict[str, int]:
        return {"requests": self.requests, "failures": self.failures}


@register(Translator, "llm")
def _factory(**kwargs: object) -> Translator:
    return LLMTranslator(**kwargs)  # type: ignore[arg-type]


# Convenience aliases so configs can say `engine = "deepseek"` etc.
for _alias in ("openai", "deepseek", "ollama", "kimi", "qwen"):

    @register(Translator, _alias)
    def _alias_factory(**kwargs: object) -> Translator:
        return LLMTranslator(**kwargs)  # type: ignore[arg-type]
