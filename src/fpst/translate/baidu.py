"""Baidu Translate (fanyi-api.baidu.com) implementation of :class:`Translator`.

Protocol (fixed by Baidu):

    POST https://api.fanyi.baidu.com/api/trans/vip/translate
    q=<text, lines joined by \n>&from=en&to=zh&appid=<id>&salt=<n>&sign=<md5>
    sign = md5(appid + q + salt + key)

Follows Baidu's own sample closely: the parameters go in the **query string**
(``params=``), which is what their example sends, and ``salt`` is a random int in
[32768, 65536]. The endpoint is upgraded to HTTPS -- the sample uses plain HTTP,
which would put the signature on the wire in clear text.

Two properties matter for this product:

* **Batching is native.** ``q`` may contain several lines separated by ``\\n``
  and the response carries one ``trans_result`` per line, which maps directly
  onto our batch interface -- one request per screen update instead of one per
  chat line.
* **The free tier allows 1 request/second.** Exceeding it returns 54003, so we
  rate-limit locally rather than hammering and failing.

Because this is a conventional MT engine there is no prompt: game slang loses the
contextual rendering an LLM gives, and the glossary can only short-circuit whole
lines (handled by CachingTranslator), not steer individual words.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from collections.abc import Sequence

from ..core.interfaces import Translator
from ..core.registry import register
from ..core.types import Lang

log = logging.getLogger(__name__)

__all__ = ["BaiduTranslator"]

ENDPOINT = "https://api.fanyi.baidu.com/api/trans/vip/translate"

LANG_CODES = {
    Lang.EN: "en",
    Lang.ZH: "zh",
    Lang.JA: "jp",
    Lang.KO: "kor",
    Lang.RU: "ru",
}

ERROR_HINTS = {
    "52001": "请求超时，请重试",
    "52002": "百度系统错误，请重试",
    "52003": "未授权用户：检查 appid 是否正确",
    "54000": "必填参数为空",
    "54001": "签名错误：检查密钥是否与 appid 匹配",
    "54003": "访问频率受限：调低 [translate] baidu_qps",
    "54004": "账户余额不足",
    "54005": "长 query 请求频繁：调低 baidu_qps",
    "58000": "客户端 IP 非法",
    "58001": "译文语言方向不支持",
    "58002": "服务当前已关闭",
    "90107": "认证未通过或未生效",
}

MAX_QUERY_BYTES = 1200
"""Parameters travel in the query string (per Baidu's sample), so keep the
batch well under typical 8 KB URL limits as well as Baidu's own size limit."""


class BaiduTranslator(Translator):
    name = "baidu"

    def __init__(
        self,
        appid: str = "",
        secret_key: str = "",
        appid_env: str = "FPST_BAIDU_APPID",
        key_env: str = "FPST_BAIDU_KEY",
        timeout_s: float = 8.0,
        qps: float = 1.0,
        transport: object | None = None,
        **_ignored: object,
    ) -> None:
        import os

        self._appid = (appid or os.environ.get(appid_env, "")).strip()
        self._key = (secret_key or os.environ.get(key_env, "")).strip()
        self._appid_env = appid_env
        self._key_env = key_env
        self._timeout = timeout_s
        self._min_gap = 1.0 / qps if qps > 0 else 0.0
        self._transport = transport
        self._client = None
        self._last_request = 0.0
        self._lock = threading.Lock()
        self.requests = 0
        self.failures = 0

    # ------------------------------------------------------------- lifecycle
    def warmup(self) -> None:
        if self._client is not None:
            return
        if not self._appid or not self._key:
            raise RuntimeError(
                f"百度翻译需要 appid 和密钥：设置 {self._appid_env} / {self._key_env}，"
                "或运行 `fpst auth set`"
            )
        import httpx

        self._client = httpx.Client(timeout=self._timeout, transport=self._transport)  # type: ignore[arg-type]

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

        from_code = LANG_CODES.get(src, "auto")
        to_code = LANG_CODES.get(dst, "zh")

        # Keep the input/answer alignment: blank lines are dropped from the
        # joined query (they would collapse) and re-inserted afterwards.
        kept: list[tuple[int, str]] = [
            (i, text.strip().replace("\n", " ")) for i, text in enumerate(texts) if text.strip()
        ]
        out = [""] * len(texts)
        if not kept:
            return out

        for chunk in self._chunks(kept):
            results = self._request([text for _, text in chunk], from_code, to_code)
            if results is None:
                for index, text in chunk:
                    out[index] = text  # degrade to the source line
                continue
            for (index, text), translated in zip(chunk, results, strict=False):
                out[index] = translated or text
        return out

    @staticmethod
    def _chunks(items: list[tuple[int, str]]):
        """Split into requests that respect Baidu's query-size limit."""
        batch: list[tuple[int, str]] = []
        size = 0
        for item in items:
            length = len(item[1].encode("utf-8")) + 1
            if batch and size + length > MAX_QUERY_BYTES:
                yield batch
                batch, size = [], 0
            batch.append(item)
            size += length
        if batch:
            yield batch

    # --------------------------------------------------------------- request
    def _request(self, lines: list[str], from_code: str, to_code: str) -> list[str] | None:
        assert self._client is not None
        query = "\n".join(lines)
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5((self._appid + query + salt + self._key).encode("utf-8")).hexdigest()

        with self._lock:
            wait = self._min_gap - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

        self.requests += 1
        try:
            response = self._client.post(
                ENDPOINT,
                params={  # query string, as in Baidu's sample
                    "q": query,
                    "from": from_code,
                    "to": to_code,
                    "appid": self._appid,
                    "salt": salt,
                    "sign": sign,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - never take the pipeline down
            self.failures += 1
            log.warning("baidu request failed (%s); showing source text", exc)
            return None

        if "error_code" in payload:
            self.failures += 1
            code = str(payload["error_code"])
            hint = ERROR_HINTS.get(code, payload.get("error_msg", ""))
            log.warning("baidu error %s: %s", code, hint)
            return None

        results = payload.get("trans_result") or []
        translated = [item.get("dst", "") for item in results]
        if len(translated) != len(lines):
            self.failures += 1
            log.warning(
                "baidu returned %d lines for %d inputs; falling back",
                len(translated),
                len(lines),
            )
            return None
        return translated

    @property
    def stats(self) -> dict[str, int]:
        return {"requests": self.requests, "failures": self.failures}


@register(Translator, "baidu")
def _factory(**kwargs: object) -> Translator:
    return BaiduTranslator(**kwargs)  # type: ignore[arg-type]
