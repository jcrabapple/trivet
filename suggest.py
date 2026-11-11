"""Live query completions from Google through Serper.

The browser only talks to Trivet's same-origin route. Serper credentials stay on
the server, successful responses share a bounded five-minute in-memory cache,
and failures are never cached as empty suggestions.
"""
from __future__ import annotations

import time
from collections import OrderedDict

import httpx

ENDPOINT = "https://google.serper.dev/autocomplete"
TTL_SECONDS = 300.0
MAX_ENTRIES = 256
MAX_SUGGESTIONS = 8
_CACHE: OrderedDict[tuple[str, int], tuple[float, tuple[str, ...]]] = OrderedDict()


def normalize(query: str) -> str:
    """Collapse whitespace and bound input before it reaches the paid upstream."""
    return " ".join(query.split())[:200].strip()


def _cached(key: tuple[str, int], now: float) -> tuple[str, ...] | None:
    row = _CACHE.get(key)
    if row is None:
        return None
    expires, values = row
    if expires <= now:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return values


def clear_cache() -> None:
    _CACHE.clear()


async def get(client: httpx.AsyncClient, query: str, api_key: str,
              limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Return clean, deduplicated suggestions; raise on upstream failure."""
    query = normalize(query)
    limit = max(1, min(MAX_SUGGESTIONS, limit))
    if not query or not api_key:
        return []
    key = (query.casefold(), limit)
    now = time.monotonic()
    cached = _cached(key, now)
    if cached is not None:
        return list(cached)

    response = await client.post(
        ENDPOINT,
        headers={"X-API-KEY": api_key, "Accept": "application/json"},
        json={"q": query, "gl": "us", "hl": "en"},
        timeout=1.5,
    )
    response.raise_for_status()
    payload = response.json()
    raw = payload.get("suggestions", [])
    if not isinstance(raw, list):
        raise ValueError("Serper autocomplete returned an invalid suggestions field")

    values: list[str] = []
    seen = {query.casefold()}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            continue
        value = normalize(item["value"])
        folded = value.casefold()
        if not value or folded in seen:
            continue
        seen.add(folded)
        values.append(value)
        if len(values) >= limit:
            break

    result = tuple(values)
    _CACHE[key] = (now + TTL_SECONDS, result)
    _CACHE.move_to_end(key)
    while len(_CACHE) > MAX_ENTRIES:
        _CACHE.popitem(last=False)
    return list(result)
