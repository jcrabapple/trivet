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
from email.utils import parsedate_to_datetime
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
    image_url: str = ""
    thumb_url: str = ""
    width: int = 0
    height: int = 0
    source_name: str = ""


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


_ALLOWED_SCHEMES = ("http://", "https://")
_REL_AGE_RE = re.compile(
    r"^(\d+)\s*(second|sec|minute|min|hour|hr|day|week|wk|month|mo|year|yr)s?\s+ago$",
    re.IGNORECASE,
)
_REL_UNIT_HOURS = {
    "second": 1 / 3600, "sec": 1 / 3600,
    "minute": 1 / 60, "min": 1 / 60,
    "hour": 1.0, "hr": 1.0,
    "day": 24.0,
    "week": 168.0, "wk": 168.0,
    "month": 730.0, "mo": 730.0,
    "year": 8760.0, "yr": 8760.0,
}


def parse_age_hours(raw: str | None, now: float | None = None) -> float | None:
    """Parse ISO, RFC-2822, calendar, or relative date strings into hours old."""
    if not raw:
        return None
    text = " ".join(str(raw).strip().split())
    if not text:
        return None
    low = text.lower()
    if low == "just now":
        return 0.05
    if low == "yesterday":
        return 24.0
    m = _REL_AGE_RE.match(low)
    if m:
        return float(m.group(1)) * _REL_UNIT_HOURS[m.group(2)]

    ref = now if now is not None else time.time()
    dt: datetime | None = None
    # ISO 8601 (with Z, offset, or naive UTC such as Brave's page_age)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    # RFC 2822 (Tavily's "Tue, 10 Nov 2026 11:00:00 GMT")
    if dt is None and "," in text:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            pass
    if dt is None:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %b %Y", "%d %B %Y"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (ref - dt.timestamp()) / 3600.0)


def fmt_news_age(raw: str | None, now: float | None = None) -> str:
    """Uniform age label for news rows: relative inside 14 days, date beyond."""
    if not raw:
        return ""
    hrs = parse_age_hours(raw, now=now)
    if hrs is None:
        return str(raw).strip()
    if hrs < 1 / 60:
        return "just now"
    if hrs < 1.0:
        return f"{max(1, round(hrs * 60))}m ago"
    if hrs < 48.0:
        return f"{max(1, round(hrs))}h ago"
    if hrs < 14 * 24.0:
        return f"{max(2, round(hrs / 24.0))}d ago"
    ref = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ref - hrs * 3600.0, tz=timezone.utc)
    return dt.strftime("%d %b %Y")


def _strip_leading_title(snippet: str, title: str) -> str:
    """Drop a repeated headline from the front of markdown-derived news snippets."""
    if not snippet or not title:
        return snippet
    for head in (title, title.split(" - ")[0], title.split(" | ")[0]):
        h = head.rstrip("….").strip()
        if len(h) >= 12 and snippet.lower().startswith(h.lower()):
            rest = snippet[len(h):].lstrip(" -–—·:|,.")
            if rest:
                return rest
    return snippet


def _hits(items, engine: str, *, url_key="url", title_key="title",
          snippet_keys=("highlights", "snippet", "description", "content", "text", "excerpt", "excerpts"),
          published_key=None) -> list[Hit]:
    out: list[Hit] = []
    for i, it in enumerate(items or [], start=1):
        if not isinstance(it, dict):
            continue
        url = str(it.get(url_key) or it.get("link") or it.get("href") or "").strip()
        if not url.lower().startswith(_ALLOWED_SCHEMES):
            continue          # a javascript: or data: URL is never a search result
        title = _clean(it.get(title_key) or it.get("name") or url, 200)
        snippet = ""
        for sk in snippet_keys:
            val = it.get(sk)
            if val:
                if isinstance(val, list):
                    val = " ".join(str(x) for x in val)
                snippet = _clean(_strip_leading_title(_clean(val, 600), title), 240)
                if snippet:
                    break
        out.append(Hit(
            title=title,
            url=url,
            snippet=snippet,
            engine=engine,
            rank=i,
            score=it.get("score") if isinstance(it.get("score"), (int, float)) else it.get("relevance_score"),
            published=it.get(published_key) if published_key else None,
        ))
    return out


def _pos_int(val: Any) -> int:
    try:
        n = int(val)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def _image_hit(rank: int, engine: str, *, title: Any, page_url: Any,
               image_url: Any, thumb_url: Any = None,
               width: Any = 0, height: Any = 0) -> Hit | None:
    img = str(image_url or "").strip()
    if not img.lower().startswith(_ALLOWED_SCHEMES):
        return None
    thumb = str(thumb_url or img).strip()
    if not thumb.lower().startswith(_ALLOWED_SCHEMES):
        thumb = img
    page = str(page_url or img).strip()
    if not page.lower().startswith(_ALLOWED_SCHEMES):
        page = img
    return Hit(
        title=_clean(title or page, 160),
        url=page,
        engine=engine,
        rank=rank,
        image_url=img,
        thumb_url=thumb,
        width=_pos_int(width),
        height=_pos_int(height),
    )


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


# ------------------------------------------------------------ image adapters
async def brave_images(client, query, limit, key, opts: QueryOpts):
    params: dict[str, Any] = {"q": query, "count": min(limit, 100)}
    freshness = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(opts.time_range)
    if freshness:
        params["freshness"] = freshness
    # Brave's image endpoint rejects safesearch="moderate" with HTTP 422; only
    # "off" and "strict" exist in its enum.
    params["safesearch"] = "off" if opts.safesearch == "off" else "strict"
    r = await client.get("https://api.search.brave.com/res/v1/images/search",
                         headers={"X-Subscription-Token": key, "Accept": "application/json"},
                         params=params)
    r.raise_for_status()
    out: list[Hit] = []
    for i, it in enumerate(r.json().get("results") or [], start=1):
        if not isinstance(it, dict):
            continue
        props = it.get("properties") or {}
        thumb = it.get("thumbnail") or {}
        h = _image_hit(
            len(out) + 1, "brave",
            title=it.get("title"),
            page_url=it.get("url"),
            image_url=props.get("url"),
            thumb_url=thumb.get("src"),
            width=props.get("width") or thumb.get("width"),
            height=props.get("height") or thumb.get("height"),
        )
        if h:
            out.append(h)
    return out, None


async def serper_images(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"q": query, "num": min(limit, 100)}
    tbs = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(opts.time_range)
    if tbs:
        body["tbs"] = tbs
    if opts.safesearch == "strict":
        body["safe"] = "active"
    r = await client.post("https://google.serper.dev/images",
                          headers={"X-API-KEY": key}, json=body)
    r.raise_for_status()
    out: list[Hit] = []
    for it in r.json().get("images") or []:
        if not isinstance(it, dict):
            continue
        h = _image_hit(
            len(out) + 1, "serper",
            title=it.get("title"),
            page_url=it.get("link"),
            image_url=it.get("imageUrl"),
            thumb_url=it.get("thumbnailUrl"),
            width=it.get("imageWidth") or it.get("thumbnailWidth"),
            height=it.get("imageHeight") or it.get("thumbnailHeight"),
        )
        if h:
            out.append(h)
    return out, None


async def firecrawl_images(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"query": query, "sources": ["images"], "limit": min(limit, 50)}
    tbs = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(opts.time_range)
    if tbs:
        body["tbs"] = tbs
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = await client.post("https://api.firecrawl.dev/v2/search",
                          headers=headers, json=body)
    r.raise_for_status()
    data = r.json().get("data") or {}
    items = data.get("images") if isinstance(data, dict) else []
    out: list[Hit] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        h = _image_hit(
            len(out) + 1, "firecrawl",
            title=it.get("title"),
            page_url=it.get("url"),
            image_url=it.get("imageUrl"),
            width=it.get("imageWidth"),
            height=it.get("imageHeight"),
        )
        if h:
            out.append(h)
    return out, None


# ------------------------------------------------------------- news adapters
async def exa_news(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {
        "query": query, "category": "news", "numResults": limit, "type": "auto",
        "contents": {"highlights": {"numSentences": 2, "highlightsPerUrl": 1}},
    }
    since = _since_iso(opts.time_range)
    if since:
        body["startPublishedDate"] = since
    r = await client.post("https://api.exa.ai/search",
                          headers={"x-api-key": key}, json=body)
    r.raise_for_status()
    return _hits(r.json().get("results"), "exa", published_key="publishedDate"), None


async def brave_news(client, query, limit, key, opts: QueryOpts):
    params: dict[str, Any] = {"q": query, "count": min(limit, 20)}
    freshness = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(opts.time_range)
    if freshness:
        params["freshness"] = freshness
    if opts.safesearch in ("off", "moderate", "strict"):
        params["safesearch"] = opts.safesearch
    r = await client.get("https://api.search.brave.com/res/v1/news/search",
                         headers={"X-Subscription-Token": key, "Accept": "application/json"},
                         params=params)
    r.raise_for_status()
    out: list[Hit] = []
    for it in r.json().get("results") or []:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        if not url.lower().startswith(_ALLOWED_SCHEMES):
            continue
        prof = it.get("profile") or {}
        outlet = _clean(prof.get("name"), 80) if isinstance(prof, dict) else ""
        title = _clean(it.get("title") or url, 200)
        snippet = _strip_leading_title(_clean(it.get("description")), title)
        out.append(Hit(
            title=title,
            url=url,
            snippet=snippet,
            engine="brave",
            rank=len(out) + 1,
            published=it.get("page_age") or it.get("age"),
            source_name=outlet,
        ))
    return out, None


async def tavily_news(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {
        "query": query, "topic": "news", "max_results": limit, "search_depth": "basic",
    }
    days = {"day": 1, "week": 7, "month": 31, "year": 366}.get(opts.time_range)
    if days:
        body["days"] = days
    r = await client.post("https://api.tavily.com/search",
                          headers={"Authorization": f"Bearer {key}"}, json=body)
    r.raise_for_status()
    return _hits(r.json().get("results"), "tavily", published_key="published_date"), None


async def serper_news(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"q": query, "num": limit}
    tbs = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(opts.time_range)
    if tbs:
        body["tbs"] = tbs
    if opts.safesearch == "strict":
        body["safe"] = "active"
    r = await client.post("https://google.serper.dev/news",
                          headers={"X-API-KEY": key}, json=body)
    r.raise_for_status()
    out: list[Hit] = []
    for it in r.json().get("news") or []:
        if not isinstance(it, dict):
            continue
        url = str(it.get("link") or it.get("url") or "").strip()
        if not url.lower().startswith(_ALLOWED_SCHEMES):
            continue
        title = _clean(it.get("title") or url, 200)
        out.append(Hit(
            title=title,
            url=url,
            snippet=_strip_leading_title(_clean(it.get("snippet")), title),
            engine="serper",
            rank=len(out) + 1,
            published=it.get("date"),
            source_name=_clean(it.get("source"), 80),
        ))
    return out, None


async def firecrawl_news(client, query, limit, key, opts: QueryOpts):
    body: dict[str, Any] = {"query": query, "sources": ["news"], "limit": min(limit, 50)}
    tbs = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(opts.time_range)
    if tbs:
        body["tbs"] = tbs
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = await client.post("https://api.firecrawl.dev/v2/search",
                          headers=headers, json=body)
    r.raise_for_status()
    data = r.json().get("data") or {}
    items = data.get("news") if isinstance(data, dict) else []
    out: list[Hit] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        if not url.lower().startswith(_ALLOWED_SCHEMES):
            continue
        title = _clean(it.get("title") or url, 200)
        raw_snip = it.get("snippet") or it.get("description") or ""
        out.append(Hit(
            title=title,
            url=url,
            snippet=_strip_leading_title(_clean(raw_snip), title),
            engine="firecrawl",
            rank=len(out) + 1,
            published=it.get("date"),
        ))
    return out, None


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
    image_fn: Callable | None = None
    news_fn: Callable | None = None


SPECS: tuple[Spec, ...] = (
    Spec("keenable", "Keenable", ("KEENABLE_API_KEY",), keenable, "fast",
         "agent-first index"),
    Spec("exa", "Exa", ("EXA_API_KEY",), exa, "fast",
         "neural index with page text", time_range=True, news_fn=exa_news),
    # Brave's free AI plan allows 1 request/second; without spacing, a page flip
    # or a retyped query returns HTTP 429 and silently drops the engine.
    Spec("brave", "Brave", ("BRAVE_API_KEY",), brave, "fast",
         "independent crawl, own ranking", time_range=True, safesearch=True,
         min_interval=1.1, image_fn=brave_images, news_fn=brave_news),
    Spec("valyu", "Valyu", ("VALYU_API_KEY",), valyu, "deep",
         "full page content, reports cost"),
    Spec("tavily", "Tavily", ("TAVILY_API_KEY",), tavily, "fast",
         "search tuned for RAG", time_range=True, news_fn=tavily_news),
    Spec("linkup", "Linkup", ("LINKUP_API_KEY",), linkup, "fast",
         "EU-hosted", time_range=True),
    Spec("serper", "Google (Serper)", ("SERPER_API_KEY",), serper, "fast",
         "Google SERP reseller", time_range=True, safesearch=True,
         image_fn=serper_images, news_fn=serper_news),
    # /v2/search answers without a key as of 2026-11-09 (measured ~0.9-1.4s, 5/5 queries);
    # a key raises the quota if they start metering it again
    Spec("firecrawl", "Firecrawl", ("FIRECRAWL_API_KEY",), firecrawl, "fast",
         "crawl-and-extract index", time_range=True, key_optional=True,
         image_fn=firecrawl_images, news_fn=firecrawl_news),
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
                     opts: QueryOpts, mode: str = "web") -> EngineResult:
    if mode == "images":
        fn = spec.image_fn
    elif mode == "news":
        fn = spec.news_fn
    else:
        fn = spec.fn
    if fn is None:
        return EngineResult(spec.name, ok=True, skipped=True, ms=0)
    key = key_for(spec, env)
    if not key and not spec.key_optional:
        return EngineResult(spec.name, ok=False, error="no key")
    await _respect_interval(spec.name, spec.min_interval)
    t0 = time.perf_counter()
    try:
        hits, cost = await asyncio.wait_for(
            fn(client, query, limit, key, opts), timeout=timeout)
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
