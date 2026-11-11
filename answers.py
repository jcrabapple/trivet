"""Grounded quick answers from one user-selected search provider.

Only question-mark searches call this module. Providers are deliberately not fused:
one engine writes the answer and supplies the citations that support it.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import httpx

TTL_SECONDS = 900.0
MAX_ENTRIES = 128
MAX_SOURCES = 8
MAX_ANSWER_CHARS = 8000
TIMEOUT_SECONDS = 35.0
# Ordered by the measured simple-question latency, with Exa first for its compact
# inline citations. Only configured members appear in Preferences.
PROVIDER_ORDER = ("exa", "tavily", "keenable", "linkup", "valyu")
ENV_KEYS = {
    "exa": "EXA_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "keenable": "KEENABLE_API_KEY",
    "linkup": "LINKUP_API_KEY",
    "valyu": "VALYU_API_KEY",
}


@dataclass
class Citation:
    title: str
    url: str


@dataclass
class AnswerResult:
    engine: str
    text: str = ""
    citations: list[Citation] = field(default_factory=list)
    ms: int = 0
    error: str = ""
    cached_age: float | None = None

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


@dataclass
class _Entry:
    created: float
    result: AnswerResult


_CACHE: OrderedDict[tuple[str, str], _Entry] = OrderedDict()


def clear_cache() -> None:
    _CACHE.clear()


def triggered(query: str) -> bool:
    """Kagi-style explicit trigger: one terminal question mark on a real query."""
    return len(query.strip()) > 1 and query.rstrip().endswith("?")


def question(query: str) -> str:
    return query.strip().rstrip("?").strip()[:512]


def available(engines: list) -> list:
    """Configured providers in stable preference order, never an unkeyed option."""
    by_name = {spec.name: spec for spec in engines}
    return [by_name[name] for name in PROVIDER_ORDER if name in by_name]


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    if len(value) <= MAX_ANSWER_CHARS:
        return value
    return value[:MAX_ANSWER_CHARS].rsplit(" ", 1)[0].rstrip() + "…"


def _citations(rows: Any) -> list[Citation]:
    out: list[Citation] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_url = row.get("url") or row.get("link") or ""
        title = row.get("title") or row.get("name") or raw_url
        # Keep the provider's positions: inline [n] markers refer to this list.
        # Invalid entries become placeholders and are omitted from the visible
        # source list rather than shifting every later citation onto a new page.
        url = raw_url if isinstance(raw_url, str) and raw_url.startswith(
            ("http://", "https://")) else ""
        out.append(Citation(" ".join(str(title).split())[:240], url[:2048]))
        if len(out) >= MAX_SOURCES:
            break
    return out


async def _exa(client: httpx.AsyncClient, query: str, key: str) -> AnswerResult:
    r = await client.post("https://api.exa.ai/answer", headers={"x-api-key": key}, json={
        "query": query,
        "model": "exa-fast",
        "systemPrompt": ("Answer directly and concisely in at most three short paragraphs. "
                         "Use inline citation markers for factual claims."),
    }, timeout=TIMEOUT_SECONDS)
    r.raise_for_status()
    data = r.json()
    return AnswerResult("exa", _text(data.get("answer")), _citations(data.get("citations")))


async def _tavily(client: httpx.AsyncClient, query: str, key: str) -> AnswerResult:
    r = await client.post("https://api.tavily.com/search",
                          headers={"Authorization": f"Bearer {key}"}, json={
        "query": query, "max_results": 5, "search_depth": "basic",
        "include_answer": "basic", "include_images": False,
    }, timeout=TIMEOUT_SECONDS)
    r.raise_for_status()
    data = r.json()
    return AnswerResult("tavily", _text(data.get("answer")), _citations(data.get("results")))


async def _keenable(client: httpx.AsyncClient, query: str, key: str) -> AnswerResult:
    r = await client.post("https://agents.keenable.ai/v1/answer",
                          headers={"X-API-Key": key}, json={
        "query": query, "effort": "low",
        "system_prompt": "Answer directly and concisely in at most three short paragraphs.",
    }, timeout=TIMEOUT_SECONDS)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        error = data["error"]
        code = error.get("code", "answer failed") if isinstance(error, dict) else str(error)
        return AnswerResult("keenable", error=str(code)[:80])
    return AnswerResult("keenable", _text(data.get("answer")), _citations(data.get("citations")))


async def _linkup(client: httpx.AsyncClient, query: str, key: str) -> AnswerResult:
    r = await client.post("https://api.linkup.so/v1/search",
                          headers={"Authorization": f"Bearer {key}"}, json={
        "q": query, "depth": "standard", "outputType": "sourcedAnswer",
        "includeInlineCitations": True, "maxResults": 5,
    }, timeout=TIMEOUT_SECONDS)
    r.raise_for_status()
    data = r.json()
    return AnswerResult("linkup", _text(data.get("answer")), _citations(data.get("sources")))


async def _valyu(client: httpx.AsyncClient, query: str, key: str) -> AnswerResult:
    chunks: list[str] = []
    sources: list[Citation] = []
    final: dict[str, Any] = {}
    stream_error = ""
    async with client.stream("POST", "https://api.valyu.ai/v1/answer",
                             headers={"x-api-key": key}, json={
        "query": query, "search_type": "web", "fast_mode": True,
        "data_max_price": 0.10,
        "system_instructions": ("Answer directly and concisely in at most three short "
                                "paragraphs. Cite sources inline."),
    }, timeout=TIMEOUT_SECONDS) as r:
        if r.status_code != 200:
            await r.aread()
            r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            if isinstance(event.get("search_results"), list):
                sources = _citations(event["search_results"])
            for choice in event.get("choices") or []:
                if isinstance(choice, dict):
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                        chunks.append(delta["content"])
            if "cost" in event or "success" in event:
                final = event
            if event.get("error"):
                stream_error = str(event["error"])[:80]
    if not chunks and isinstance(final.get("contents"), str):
        chunks.append(final["contents"])
    if isinstance(final.get("search_results"), list):
        sources = _citations(final["search_results"])
    return AnswerResult("valyu", _text("".join(chunks)), sources, error=stream_error)


ADAPTERS: dict[str, Callable] = {
    "exa": _exa,
    "tavily": _tavily,
    "keenable": _keenable,
    "linkup": _linkup,
    "valyu": _valyu,
}


async def get(client: httpx.AsyncClient, engine: str, query: str,
              env: dict[str, str], use_cache: bool = True) -> AnswerResult:
    """Return one answer or a compact error; never break the surrounding search."""
    query = question(query)
    key = (engine, query.casefold())
    now = time.monotonic()
    if use_cache:
        entry = _CACHE.get(key)
        if entry and now - entry.created < TTL_SECONDS:
            _CACHE.move_to_end(key)
            return replace(entry.result, ms=0, cached_age=now - entry.created)
        _CACHE.pop(key, None)
    if engine not in ADAPTERS or not query:
        return AnswerResult(engine, error="unsupported answer engine")
    api_key = env.get(ENV_KEYS[engine], "")
    if not api_key:
        return AnswerResult(engine, error="API key unavailable")
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(ADAPTERS[engine](client, query, api_key),
                                        timeout=TIMEOUT_SECONDS)
        result.ms = int((time.perf_counter() - started) * 1000)
        if not result.text and not result.error:
            result.error = "empty answer"
    except httpx.HTTPStatusError as exc:
        result = AnswerResult(engine, ms=int((time.perf_counter() - started) * 1000),
                              error=f"HTTP {exc.response.status_code}")
    except (httpx.TimeoutException, asyncio.TimeoutError):
        result = AnswerResult(engine, ms=int((time.perf_counter() - started) * 1000),
                              error="timeout")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        result = AnswerResult(engine, ms=int((time.perf_counter() - started) * 1000),
                              error=type(exc).__name__)
    if result.ok:
        _CACHE[key] = _Entry(now, replace(result))
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)
    return result
