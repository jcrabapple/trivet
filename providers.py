"""Engine adapters for trivet.

Every backend normalizes to `Hit`. Adapters never raise on a per-query failure:
they return an error string so one dead engine cannot take down a search, and so
a stale key is visible instead of silently returning zero results.
"""
from __future__ import annotations

import asyncio
import glob
import html as html_module
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

UA = "trivet/0.2 (+local)"
# Infisical is optional: set both variables to pull keys from a vault as well as
# from files and the web UI. Unset means the integration is skipped entirely.
INFISICAL_PROJECT = os.environ.get("TRIVET_INFISICAL_PROJECT", "")
INFISICAL_TOKEN = os.path.expanduser(
    os.environ.get("TRIVET_INFISICAL_TOKEN_FILE", "~/.cache/trivet/infisical-token"))

TIME_RANGES = ("any", "day", "week", "month", "year")


# ---------------------------------------------------------------- env loading
def _read_env_file(env: dict[str, str], path: str) -> None:
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if v:
                    env.setdefault(k, v)
    except OSError:
        pass


def load_infisical(envs: tuple[str, ...] = ("dev", "prod"),
                   ttl: int = 3600) -> dict[str, str]:
    """Read secrets from the Infisical project, cached on disk for an hour.

    Opt-in via TRIVET_INFISICAL_PROJECT + TRIVET_INFISICAL_TOKEN_FILE. The disk cache
    exists because two HTTPS round trips at import time delay every process start.
    """
    if not INFISICAL_PROJECT:
        return {}
    cache_dir = os.path.expanduser("~/.cache/trivet")
    cache_path = os.path.join(cache_dir, "secrets.json")
    try:
        cached = json.load(open(cache_path))
        if time.time() - cached.get("_ts", 0) < ttl:
            return {k: v for k, v in cached.items() if not k.startswith("_")}
    except (OSError, ValueError):
        pass

    out: dict[str, str] = {}
    try:
        token = open(INFISICAL_TOKEN).read().strip()
    except OSError:
        return out
    for name in envs:
        url = (f"https://app.infisical.com/api/v3/secrets/raw"
               f"?workspaceId={INFISICAL_PROJECT}&environment={name}&secretPath=/")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            data = json.load(urllib.request.urlopen(req, timeout=8))
        except Exception:  # noqa: BLE001 - Infisical down must not break search
            continue
        for s in data.get("secrets", []):
            if s.get("secretValue"):
                out.setdefault(s["secretKey"], s["secretValue"])
    if out:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path, "w") as fh:
                json.dump({**out, "_ts": time.time()}, fh)
            os.chmod(cache_path, 0o600)
        except OSError:
            pass
    return out


def load_env() -> dict[str, str]:
    """Layer env files, keys entered in the web UI, then the process environment.

    Precedence is deliberate: a key typed into Settings beats a stale line in a
    dotfile, and an explicit export beats both. Stored keys that cannot be
    decrypted (rotated secret) are skipped, so the remaining sources still apply.
    """
    env: dict[str, str] = {}
    for path in ([os.path.expanduser(".env"), os.path.expanduser("~/.config/trivet/trivet.env")]
                 + sorted(glob.glob(os.path.expanduser("~/.config/environment.d/*.conf")))):
        _read_env_file(env, path)
    try:
        import secrets_store
        env.update({k: v for k, v in secrets_store.load().items() if v})
    except Exception:  # noqa: BLE001 - no cryptography or no DB is not fatal
        pass
    env.update(load_infisical())
    env.update({k: v for k, v in os.environ.items()
                if k.endswith(("_API_KEY", "_API_TOKEN")) and v})
    return env


@dataclass
class QueryOpts:
    time_range: str = "any"       # any|day|week|month|year
    safesearch: str = "moderate"  # off|moderate|strict


def _since_iso(time_range: str) -> str | None:
    days = {"day": 1, "week": 7, "month": 31, "year": 366}.get(time_range)
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    rank: int = 0
    score: float | None = None
    published: str | None = None


@dataclass
class EngineResult:
    name: str
    ok: bool
    hits: list[Hit] = field(default_factory=list)
    ms: int = 0
    error: str | None = None
    cost_usd: float | None = None
    # deep engines can be held back when the fast tier already filled the page;
    # "skipped" is not "failed" and must not be counted as one
    skipped: bool = False


def _clean(text: Any, limit: int = 240) -> str:
    """Normalize engine text for display: drop markdown artifacts, keep whole words.

    Agentic engines return page markdown, so snippets arrive with `####`, `**bold**`
    and `](url)` residue that looks like a bug in a search result. Truncate on a word
    boundary with an ellipsis so lines never end mid-parenthesis.
    """
    if not text:
        return ""
    s = " ".join(str(text).split())
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)   # [text](url) -> text
    if "<" in s:                                          # engine descriptions ship HTML
        s = re.sub(r"<[^>]{1,200}>", " ", s)
        s = html_module.unescape(s)
    s = re.sub(r"[#*_`>]+", "", s)                        # markdown punctuation
    s = " ".join(s.split()).lstrip("-–—•·|,;: ")          # list/bullet residue
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-(")
    return cut + "…"


def _hits(items, engine: str, *, url_key="url", title_key="title",
          snippet_keys=("snippet", "description", "content", "text", "excerpt", "excerpts"),
          published_key=None) -> list[Hit]:
    out: list[Hit] = []
    for i, it in enumerate(items or [], start=1):
        if not isinstance(it, dict):
            continue
        url = it.get(url_key) or it.get("link") or it.get("href") or ""
        if not url:
            continue
        snippet = ""
        for sk in snippet_keys:
            val = it.get(sk)
            if val:
                if isinstance(val, list):
                    val = " ".join(str(x) for x in val)
                snippet = _clean(val)
                if snippet:
                    break
        out.append(Hit(
            title=_clean(it.get(title_key) or it.get("name") or url, 200),
            url=url,
            snippet=snippet,
            engine=engine,
            rank=i,
            score=it.get("score") if isinstance(it.get("score"), (int, float)) else it.get("relevance_score"),
            published=it.get(published_key) if published_key else None,
        ))
    return out


# ------------------------------------------------------------------- adapters
async def keenable(client, query, limit, key, opts: QueryOpts):
    r = await client.post(
        "https://api.keenable.ai/v1/search",
        headers={"Authorization": f"Bearer {key}", "X-Keenable-Title": UA},
        json={"query": query, "max_results": limit},
    )
    r.raise_for_status()
    return _hits(r.json().get("results"), "keenable"), None


async def exa(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {
        "query": query, "numResults": limit, "type": "auto",
        "contents": {"text": {"maxCharacters": 500}},
    }
    since = _since_iso(opts.time_range)
    if since:
        body["startPublishedDate"] = since
    r = await client.post("https://api.exa.ai/search",
                          headers={"x-api-key": key}, json=body)
    r.raise_for_status()
    return _hits(r.json().get("results"), "exa", published_key="publishedDate"), None


async def valyu(client, query, limit, key, opts: QueryOpts):
    r = await client.post(
        "https://api.valyu.ai/v1/search",
        headers={"x-api-key": key},
        json={"query": query, "search_type": "web", "max_num_results": limit},
    )
    r.raise_for_status()
    body = r.json()
    return (_hits(body.get("results"), "valyu", published_key="publication_date"),
            body.get("total_deduction_dollars"))


async def brave(client, query, limit, key, opts: QueryOpts):
    params: dict[str, Any] = {"q": query, "count": min(limit, 20)}
    freshness = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(opts.time_range)
    if freshness:
        params["freshness"] = freshness
    if opts.safesearch in ("off", "moderate", "strict"):
        params["safesearch"] = opts.safesearch
    r = await client.get("https://api.search.brave.com/res/v1/web/search",
                         headers={"X-Subscription-Token": key, "Accept": "application/json"},
                         params=params)
    r.raise_for_status()
    return _hits((r.json().get("web") or {}).get("results"), "brave",
                 published_key="age"), None


async def tavily(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"query": query, "max_results": limit, "search_depth": "basic"}
    days = {"day": 1, "week": 7, "month": 31, "year": 366}.get(opts.time_range)
    if days:
        body["days"] = days
    r = await client.post("https://api.tavily.com/search",
                          headers={"Authorization": f"Bearer {key}"}, json=body)
    r.raise_for_status()
    return _hits(r.json().get("results"), "tavily", published_key="published_date"), None


async def linkup(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"q": query, "depth": "standard", "outputType": "searchResults"}
    since = _since_iso(opts.time_range)
    if since:
        body["fromDate"] = since
    r = await client.post("https://api.linkup.so/v1/search",
                          headers={"Authorization": f"Bearer {key}"}, json=body)
    r.raise_for_status()
    data = r.json()
    return _hits(data.get("results") if isinstance(data, dict) else data, "linkup"), None


async def parallel(client, query, limit, key, opts: QueryOpts):
    r = await client.post(
        "https://api.parallel.ai/v1beta/search",
        headers={"x-api-key": key},
        json={"objective": query, "search_queries": [query],
              "processor": "base", "max_results": limit},
    )
    r.raise_for_status()
    body = r.json()
    return _hits(body.get("results") or body.get("data"), "parallel"), None


async def serper(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"q": query, "num": limit}
    tbs = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(opts.time_range)
    if tbs:
        body["tbs"] = tbs
    if opts.safesearch == "strict":
        body["safe"] = "active"
    r = await client.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": key}, json=body)
    r.raise_for_status()
    return _hits(r.json().get("organic"), "serper"), None


async def firecrawl(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"query": query, "limit": limit}
    tbs = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(opts.time_range)
    if tbs:
        body["tbs"] = tbs
    # no key is a valid configuration today, and "Bearer " with an empty value is a
    # protocol error rather than a 401, so the header has to be conditional
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = await client.post("https://api.firecrawl.dev/v2/search",
                          headers=headers, json=body)
    r.raise_for_status()
    data = r.json().get("data") or []
    if isinstance(data, dict):
        data = data.get("web") or []
    return _hits(data, "firecrawl"), None


@dataclass(frozen=True)
class Spec:
    name: str
    label: str
    env: tuple[str, ...]
    fn: Callable
    tier: str                    # "fast" | "deep"
    blurb: str
    time_range: bool = False
    safesearch: bool = False
    min_interval: float = 0.0    # seconds between calls; some plans cap at 1 rps
    key_optional: bool = False   # endpoint answers without a key (unmetered today)


SPECS: tuple[Spec, ...] = (
    Spec("keenable", "Keenable", ("KEENABLE_API_KEY",), keenable, "fast",
         "agent-first index"),
    Spec("exa", "Exa", ("EXA_API_KEY",), exa, "fast",
         "neural index with page text", time_range=True),
    # Brave's free AI plan allows 1 request/second; without spacing, a page flip
    # or a retyped query returns HTTP 429 and silently drops the engine.
    Spec("brave", "Brave", ("BRAVE_API_KEY",), brave, "fast",
         "independent crawl, own ranking", time_range=True, safesearch=True,
         min_interval=1.1),
    Spec("valyu", "Valyu", ("VALYU_API_KEY",), valyu, "deep",
         "full page content, reports cost"),
    Spec("tavily", "Tavily", ("TAVILY_API_KEY",), tavily, "fast",
         "search tuned for RAG", time_range=True),
    Spec("linkup", "Linkup", ("LINKUP_API_KEY",), linkup, "fast",
         "EU-hosted", time_range=True),
    Spec("serper", "Google (Serper)", ("SERPER_API_KEY",), serper, "fast",
         "Google SERP reseller", time_range=True, safesearch=True),
    # /v2/search answers without a key as of 2026-11-09 (measured ~0.9-1.4s, 5/5 queries);
    # a key raises the quota if they start metering it again
    Spec("firecrawl", "Firecrawl", ("FIRECRAWL_API_KEY",), firecrawl, "fast",
         "crawl-and-extract index", time_range=True, key_optional=True),
    Spec("parallel", "Parallel", ("PARALLEL_API_KEY",), parallel, "deep",
         "objective-driven search"),
)

BY_NAME = {s.name: s for s in SPECS}
# Measured with eval/combo_bench.py on 26 labelled queries (2026-11-09). The set that
# maximises recall inside ~1.3s: adding Tavily/Linkup grows the candidate pool without
# surfacing an expected page that these four missed, and both cost latency.
RECOMMENDED = ("keenable", "exa", "brave", "serper")
RECOMMENDED_NOTE = ("96% of expected answers in the top 20, in about 1.3s (26 labelled queries).")
RECOMMENDED_CAVEAT = ("Tavily and Linkup add candidates without adding recall, and cost latency. "
                      "Firecrawl returned HTTP 429 on 60% of back-to-back calls.")
DEFAULT_ENGINES = RECOMMENDED


def key_for(spec: Spec, env: dict[str, str]) -> str:
    for name in spec.env:
        if env.get(name):
            return env[name]
    return ""


def available(env: dict[str, str]) -> list[Spec]:
    return [s for s in SPECS if key_for(s, env) or s.key_optional]


_THROTTLE: dict[str, tuple[asyncio.Lock, float]] = {}


async def _respect_interval(name: str, min_interval: float) -> None:
    """Serialize calls to an engine whose plan caps requests per second."""
    if min_interval <= 0:
        return
    entry = _THROTTLE.get(name)
    if entry is None:
        entry = (asyncio.Lock(), 0.0)
        _THROTTLE[name] = entry
    lock, last = entry
    async with lock:
        wait = last + min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _THROTTLE[name] = (lock, time.monotonic())


async def run_engine(client: httpx.AsyncClient, spec: Spec, query: str, limit: int,
                     env: dict[str, str], timeout: float,
                     opts: QueryOpts) -> EngineResult:
    key = key_for(spec, env)
    if not key and not spec.key_optional:
        return EngineResult(spec.name, ok=False, error="no key")
    await _respect_interval(spec.name, spec.min_interval)
    t0 = time.perf_counter()
    try:
        hits, cost = await asyncio.wait_for(
            spec.fn(client, query, limit, key, opts), timeout=timeout)
        return EngineResult(spec.name, ok=True, hits=hits,
                            ms=int((time.perf_counter() - t0) * 1000), cost_usd=cost)
    except httpx.HTTPStatusError as e:
        return EngineResult(spec.name, ok=False,
                            ms=int((time.perf_counter() - t0) * 1000),
                            error=f"HTTP {e.response.status_code}")
    except httpx.TimeoutException:
        return EngineResult(spec.name, ok=False,
                            ms=int((time.perf_counter() - t0) * 1000), error="timeout")
    except Exception as e:  # noqa: BLE001 - one dead engine must not kill the search
        return EngineResult(spec.name, ok=False,
                            ms=int((time.perf_counter() - t0) * 1000),
                            error=f"{type(e).__name__}")
