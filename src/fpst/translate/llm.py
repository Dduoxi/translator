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

# Human-readable names for the prompt. "zh" alone is ambiguous (the model may
# pick Traditional Chinese or English-influenced wording).
_LANG_NAMES = {
    Lang.EN: "English",
    Lang.ZH: "Simplified Chinese",
    Lang.JA: "Japanese",
    Lang.KO: "Korean",
    Lang.RU: "Russian",
}

SYSTEM_PROMPT = """You are the subtitle translator for live multiplayer games.
You turn in-game chat and voice comms into short {dst} subtitles that a player
can read while actually playing -- shooters, MOBAs, hero shooters alike.

Translate from {src} into {dst}.

Hard rules:
- Output ONLY the translation. No notes, no quotes, no romanisation.
- SHORT subtitles. Aim under 20 characters. Drop filler a player does not need
  mid-match ("please", "I think", "you know", "can you").
- Read abbreviations as a teammate would. Praise: gg, wp, nt, gj. Ability or
  resource state: oom, cd, ult, no ult. Actions: gank, roam, dive, push, peel.
  When an abbreviation is ambiguous, choose the meaning a teammate needs to act
  on, not the dictionary sense.
- Keep player names, clan tags, numbers, hero/champion names and map codes
  exactly as written: PlayerOne, xX_Shadow_Xx, Tracer, 2hp, B site, A main.
- Keep the terse, spoken register of voice comms. Never formal or literary.
- The input comes from OCR and may contain garbled fragments. Silently ignore
  them; never guess or invent words that are not there.
- Return "" (empty string) for lines with no message to translate: pure
  numbers or symbols, kill-feed counters, or a line that is only a name.
- Never merge, split or reorder lines. Exactly one output per input.

Examples (input -> output):
["gg wp", "rush b fast i have flash"] -> ["打得好", "快攻B点，我有闪"]
["oom no mana, need 30s", "gank mid now"] -> ["没蓝了，等30秒", "现在抓中路"]
["xX_Shadow_Xx: 2hp behind the box", "nice shot"] -> ["xX_Shadow_Xx：残血，箱子后", "好枪"]
["+150", "ez", "PlayerOne"] -> ["", "简单", ""]

Game context (may be empty; it overrides the general rules above):
{context}

Glossary (use these renderings when the term appears):
{glossary}

Output: one JSON array of exactly {count} strings, in input order.
No markdown fences, no other text."""


def _mentions(haystack: str, term: str) -> bool:
    """Whole-word match, not substring.

    Substring matching is wrong for a glossary full of short abbreviations:
    "gg" occurs inside "aggressive", so a naive ``in`` check would inject
    ``gg = 打得好`` into a line that has nothing to do with it.
    """
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack, re.IGNORECASE) is not None


def _lang_name(lang: Lang) -> str:
    return _LANG_NAMES.get(lang, str(lang.value))


class LLMTranslator(Translator):
    name = "llm"

    def __init__(
        self,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        api_key: str | None = None,
        api_key_env: str = "FPST_API_KEY",
        glossary: dict[str, str] | None = None,
        timeout_s: float = 20.0,
        retries: int = 1,
        retry_backoff_s: float = 0.4,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        context: str = "",
        transport: object | None = None,
        disable_thinking: bool = True,
        extra_params: dict[str, object] | None = None,
        **_ignored: object,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key or os.environ.get(api_key_env, "").strip()
        self._api_key_env = api_key_env
        self._glossary = {k: v for k, v in (glossary or {}).items() if k}
        self._timeout = timeout_s
        self._retries = max(0, int(retries))
        self._backoff = max(0.0, retry_backoff_s)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._context = context.strip() or "(none)"
        self._disable_thinking = disable_thinking
        self._extra_params = dict(extra_params or {})
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

        payload: dict[str, object] = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._token_budget(texts),
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        src=_lang_name(src),
                        dst=_lang_name(dst),
                        context=self._context,
                        glossary=self._glossary_text(texts),
                        count=len(texts),
                    ),
                },
                {"role": "user", "content": json.dumps(list(texts), ensure_ascii=False)},
            ],
        }

        if self._disable_thinking and self._model.startswith("deepseek"):
            # DeepSeek V4 reasons by default, and reasoning is fatal here:
            # measured over 5 identical requests it fired 3 times, adding 0.6 s
            # and once consuming the entire token budget, returning an EMPTY
            # content field -- i.e. no subtitle at all. temperature=0 does not
            # stop it. `thinking` is the switch that actually works (5/5
            # suppressed, 1483 ms -> 880 ms median).
            #
            # `effort: low` is NOT the answer: it made things worse (5/5
            # reasoning) because it caps effort rather than disabling thought.
            payload["thinking"] = {"type": "disabled"}
        payload.update(self._extra_params)

        self.requests += 1
        content = self._post_with_retries(payload)
        if content is None:
            self.failures += 1
            log.warning("translation unavailable; showing source text instead")
            return list(texts)

        parsed = self._parse(content, list(texts))
        if parsed is None:
            self.failures += 1
            log.warning("could not parse model output: %r", content[:200])
            return list(texts)
        return parsed

    def _post_with_retries(self, payload: dict[str, object]) -> str | None:
        """Return the message content, or None when every attempt failed.

        The slow tail on hosted LLMs is transient (measured: the same prompt
        took 0.8 s and 28 s minutes apart), so one retry recovers most of them.
        We deliberately do not retry 4xx: a bad key or bad model name will
        never succeed and retrying only doubles the delay.
        """
        import httpx

        assert self._client is not None
        attempts = self._retries + 1
        for attempt in range(attempts):
            try:
                response = self._client.post(f"{self._base_url}/chat/completions", json=payload)
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retryable = status >= 500 or status == 429
                log.debug("attempt %d/%d failed: HTTP %s", attempt + 1, attempts, status)
                if not retryable or attempt == attempts - 1:
                    return None
            except Exception as exc:  # noqa: BLE001 - timeouts, DNS, bad JSON
                log.debug("attempt %d/%d failed: %s", attempt + 1, attempts, exc)
                if attempt == attempts - 1:
                    return None
            if self._backoff:
                import time as _time

                _time.sleep(self._backoff * (attempt + 1))
        return None

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
        """Align the model's list to our input.

        When the count matches, an empty string is *honoured*: the prompt asks
        the model to return "" for noise lines (kill-feed counters, lone
        names), and the pipeline drops empty translations. Only when the count
        is wrong do we treat empties as damage and fall back to the source.
        """
        if len(items) == len(fallback):
            return [item.strip() for item in items]
        log.debug("model returned %d items for %d inputs", len(items), len(fallback))
        out = []
        for i, source in enumerate(fallback):
            value = items[i].strip() if i < len(items) else ""
            out.append(value or source)
        return out

    def _glossary_text(self, texts: Sequence[str] | None = None) -> str:
        """Only inject terms that actually appear in this batch.

        A 40+ term glossary pasted into every request is pure token waste and
        distracts the model. Matching is whole-word (see :func:`_mentions`),
        because short abbreviations are substrings of ordinary words.
        """
        if not self._glossary:
            return "(none)"
        if texts:
            haystack = " ".join(texts)
            relevant = {
                term: value for term, value in self._glossary.items() if _mentions(haystack, term)
            }
            if relevant:
                return ", ".join(f"{k} = {v}" for k, v in relevant.items())
            # Nothing matched, so a glossary cannot help this batch. Injecting
            # all 40+ terms anyway would be pure token waste and noise.
            return "(no glossary term appears in this batch)"
        return ", ".join(f"{k} = {v}" for k, v in self._glossary.items())

    def _token_budget(self, texts: Sequence[str]) -> int:
        """Scale the cap with the batch so long batches are never truncated.

        A fixed cap silently cuts the array mid-way, which then fails JSON
        parsing and degrades the whole batch to the source text.
        """
        estimate = sum(len(t) for t in texts) * 2 + 64
        # Floor of 512, not 256: max_tokens is a ceiling and unused tokens are
        # not billed, but a model that decides to reason anyway would burn the
        # whole budget on thoughts and return an empty answer.
        return max(512, min(self._max_tokens, estimate))

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
