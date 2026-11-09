#!/usr/bin/env python3
"""trivet — a human search engine over agentic search indexes.

Run:
    env -u PYTHONPATH python3 app.py --port 8952
CLI:
    env -u PYTHONPATH python3 app.py --cli "query"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hmac
import os
import sys
import threading
import time
from dataclasses import asdict
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import auth
import bangs
import cache
import feedback
import ranking
import reputation
import smallweb
import stats
import render
import secrets_store
from merge import MergedHit, engine_consensus, merge
from providers import (BY_NAME, DEFAULT_ENGINES, SPECS, EngineResult, QueryOpts,
                       available, load_env, run_engine)

PER_CHOICES = (10, 20, 30)
ENGINE_TIMEOUT = 4.0   # cap so one slow engine cannot hold the page past ~4s
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# Optional HTTP Basic auth, set by the systemd EnvironmentFile. This engine spends
# real money per query, so an exposed instance must not be anonymous. Unset = open,
# which is only appropriate for localhost.
AUTH_USER = os.environ.get("TRIVET_USER", "")
AUTH_PASS = os.environ.get("TRIVET_PASS", "")
PUBLIC_PATHS = {"/healthz", "/favicon.svg", "/login", "/logout"}

# Filled in by __main__ so request handling can tell a local development instance
# apart from one exposed on an interface. Localhost is the only place where
# running without credentials is defensible.
BIND_HOST = os.environ.get("TRIVET_BIND_HOST", "127.0.0.1")


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost", "")


def _check_auth_config(host: str) -> None:
    """Fail at startup rather than open at runtime.

    Two ways this used to go wrong quietly: half the pair set (leaving the app
    wide open), and no credentials at all on a non-loopback bind.
    """
    if bool(AUTH_USER) != bool(AUTH_PASS):
        raise SystemExit(
            "Set both TRIVET_USER and TRIVET_PASS, or neither: a half-configured "
            "credential pair would leave the instance open.")
    if not (AUTH_USER and AUTH_PASS):
        if not _loopback(host):
            raise SystemExit(
                f"Refusing to bind {host} with no credentials. Every search spends "
                "the operator's API budget. Set TRIVET_USER and TRIVET_PASS.")
        print("WARNING: authentication disabled, bound to loopback only",
              file=sys.stderr, flush=True)

from _version import __version__   # noqa: E402  (kept next to the app for brevity)

app = FastAPI(title="trivet", version=__version__, docs_url="/api/docs")


def _refresh_datasets() -> None:
    for mod in (smallweb, bangs):
        try:
            mod.ensure()
        except Exception:  # noqa: BLE001 - datasets are optional enrichment
            pass


threading.Thread(target=_refresh_datasets, daemon=True).start()

ENV = load_env()
ENGINES = available(ENV)


def _reload_engines() -> None:
    """Re-derive the engine list after a key is added or removed, so a new engine
    shows up without restarting the service."""
    global ENV, ENGINES
    ENV = load_env()
    ENGINES = available(ENV)
# One pooled client for the process: per-request clients pay a TLS handshake to
# every engine, which dominated query latency (~2s for three sub-second engines).
CLIENT = httpx.AsyncClient(
    timeout=15.0, headers={"User-Agent": "trivet/0.2 (+local)"},
    # No keep-alive reuse: an engine that closes an idle socket leaves httpx
    # holding a dead connection, and the next search stalls until that socket
    # errors (measured 2-4s on Exa). A fresh TLS handshake costs ~150ms and is
    # predictable, which matters more than the handshake for a search page.
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=0))


def _basic_ok(header: str) -> bool:
    if header[:6].lower() != "basic ":
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Cookie session first, then Basic auth for scripts, then a redirect to /login.

    API paths keep returning 401 rather than a redirect: a client posting a click
    beacon should get a clean failure, not an HTML login page.
    """
    path = request.url.path
    if not (AUTH_USER and AUTH_PASS) or path in PUBLIC_PATHS:
        return await call_next(request)
    if auth.verify_token(_session(request)):
        return await call_next(request)
    if _basic_ok(request.headers.get("authorization", "")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "auth required"}, status_code=401)
    nxt = quote(path + (("?" + request.url.query) if request.url.query else ""))
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Registered after require_auth, so it also wraps redirects and 401s.

    The page has one inline <style> and one inline <script> and loads nothing
    external, so a strict policy costs nothing. `unsafe-inline` is required by the
    inline beacon; frame-ancestors, base-uri and form-action still bite.
    """
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


# ------------------------------------------------------------------ helpers
def _cookie(request: Request, name: str, default: str = "") -> str:
    """tv_* is current; ms_* is the pre-rename prefix, still read so existing
    preference cookies and sessions survive the rename."""
    return request.cookies.get(f"tv_{name}") or request.cookies.get(f"ms_{name}", default)


_TRUSTED_PROXIES = {"127.0.0.1", "::1"}


def _client_ip(request: Request) -> str:
    """Bucket key for throttling and failure counting.

    A forwarding header is only believed when the immediate peer is the local
    proxy. Trusting it unconditionally let a client mint a new bucket per request
    (throttle bypass) or share one with everybody (global lockout).
    """
    peer = request.client.host if request.client else "unknown"
    if peer in _TRUSTED_PROXIES:
        forwarded = (request.headers.get("cf-connecting-ip")
                     or request.headers.get("x-forwarded-for", "").split(",")[0].strip())
        if forwarded:
            return forwarded
    return peer


# --- search rate limit: every query costs money at a vendor ------------------
_RECENT: dict[str, list[float]] = {}
RATE_LIMIT, RATE_WINDOW = 40, 60.0


def _rate_ok(bucket: str) -> bool:
    now = time.time()
    hits = _RECENT.get(bucket, [])
    hits = [t for t in hits if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        _RECENT[bucket] = hits
        return False
    hits.append(now)
    _RECENT[bucket] = hits
    if len(_RECENT) > 2000:            # keep the dict from growing without bound
        for key in [k for k, v in _RECENT.items() if not v or now - v[-1] > RATE_WINDOW]:
            _RECENT.pop(key, None)
    return True


def _csrf(request: Request) -> str:
    """Token bound to the session, so a cross-site POST cannot add or drop a key."""
    tok = _session(request)
    if not tok:
        return ""
    return hmac.new(tok.encode(), b"trivet-csrf", "sha256").hexdigest()[:24]


def _session(request: Request) -> str:
    return (request.cookies.get(auth.COOKIE_NAME)
            or request.cookies.get("ms_session", ""))


def _pick_engines(request: Request, raw: str | None) -> list[str]:
    valid = {s.name for s in ENGINES}
    if raw is not None:
        chosen = [e for e in raw.split(",") if e in valid]
    else:
        stored = _cookie(request, "engines")
        chosen = [e for e in stored.split(",") if e in valid] if stored else []
    if not chosen:
        chosen = [e for e in DEFAULT_ENGINES if e in valid] or list(valid)
    return chosen


def _prefs(request: Request, ss: str | None, per: int | None,
           time_range: str | None) -> tuple[str, int, str]:
    ss = ss or _cookie(request, "ss", "moderate")
    if ss not in ("off", "moderate", "strict"):
        ss = "moderate"
    per = per or int(_cookie(request, "per", "10") or 10)
    if per not in PER_CHOICES:
        per = 10
    time_range = time_range or _cookie(request, "time", "any")
    if time_range not in ("any", "day", "week", "month", "year"):
        time_range = "any"
    return ss, per, time_range


CACHE_VERSION = 2   # 2 = candidates carry per-engine `sources`


def _from_cache(payload: dict) -> tuple[list, list] | None:
    if payload.get("v") != CACHE_VERSION:
        return None                      # older row shape: treat as a miss
    merged = [MergedHit(**h) for h in payload["merged"]]
    engines = [EngineResult(name=e["name"], ok=e["ok"], ms=e["ms"],
                        error=e["error"], cost_usd=e["cost_usd"],
                        skipped=e.get("skipped", False))
           for e in payload["engines"]]
    return merged, engines


async def _run_specs(specs, query, fetch, opts, results_out):
    return await asyncio.gather(*[
        run_engine(CLIENT, s, query, fetch, ENV,
                   10.0 if s.tier == "deep" else ENGINE_TIMEOUT, opts) for s in specs])


MAX_QUERY = 512


async def search(query: str, engine_names: list[str], page: int, per: int,
                 ss: str, time_range: str, use_cache: bool = True,
                 ranker: str = "v1", deep_policy: str = "auto") -> dict[str, Any]:
    query = query[:MAX_QUERY]
    specs = [BY_NAME[n] for n in engine_names if n in BY_NAME]
    fetch = min(50, max(per, page * per))
    key = cache.make_key(query, [s.name for s in specs], time_range, ss, fetch,
                         deep_policy)

    if use_cache:
        hit = cache.get(key)
        cached = _from_cache(hit) if hit else None
        if cached:
            merged, engines = cached
            reps = await reputation.check_domains([h.url for h in merged])
            ranked = _order(merged, query, ranker, reps)
            return {"results": engines, "merged": merged, "ranked": ranked,
                    "reps": reps,
                    "window": [r.hit for r in ranked[(page - 1) * per: page * per]],
                    "elapsed": 0.0, "fetch": fetch, "cached_age": hit["age_s"]}

    opts = QueryOpts(time_range=time_range, safesearch=ss)
    t0 = time.perf_counter()
    # Deep-tier engines are agentic retrievers that fetch and extract page content
    # server-side: ~6s cold, against ~0.2s for the fast tier, and Valyu's whole
    # payload lands only when it finishes. So run the fast tier first and only pay
    # for the deep tier when it can add something -- fewer results than one page.
    fast_specs = [s for s in specs if s.tier != "deep"]
    deep_specs = [s for s in specs if s.tier == "deep"]
    fast_results = await _run_specs(fast_specs, query, fetch, opts, None) if fast_specs else []
    results = list(fast_results)
    if deep_specs:
        # "came up short" means something concrete: an engine failed, or the fast
        # tier could not fill a page. Counting hits alone never fires, because Exa
        # returns a full page for anything, including nonsense.
        failed = any(not r.ok for r in fast_results)
        thin = failed or (len(merge(list(fast_results))) < per) if fast_specs else True
        if deep_policy == "always" or thin:
            results += await _run_specs(deep_specs, query, fetch, opts, None)
        else:
            results += [EngineResult(name=s.name, ok=True, skipped=True, ms=0)
                        for s in deep_specs]
    # syndicated-copy collapsing stays off: it can pair one site's title with
    # another site's URL, which reads as broken to a human. Exact-URL dedupe (below)
    # is always on; duplicates are surfaced rather than hidden when collapse is used.
    merged = merge(list(results), collapse_syndicated=False)
    elapsed = time.perf_counter() - t0

    # cache only a fully successful run; otherwise a transient 401/5xx would be
    # replayed for the whole TTL and hide a key that has since recovered
    if all(r.ok for r in results):
        # Store the *unranked* candidate set: ranking runs on every request, so a
        # weight change applies to already-cached queries instead of waiting out the TTL.
        cache.put(key, cache.normalize_query(query), {
            "v": CACHE_VERSION,
            "merged": [asdict(h) for h in merged],
            "engines": [{"name": r.name, "ok": r.ok, "ms": r.ms,
                         "error": r.error, "cost_usd": r.cost_usd,
                         "skipped": r.skipped} for r in results],
        })

    stats.record(results)
    reps = await reputation.check_domains([h.url for h in merged])
    ranked = _order(merged, query, ranker, reps)
    return {"results": results, "merged": merged, "ranked": ranked, "reps": reps,
            "window": [r.hit for r in ranked[(page - 1) * per: page * per]],
            "elapsed": elapsed, "fetch": fetch, "cached_age": None}


def _order(merged: list, query: str, ranker: str, reps: dict | None = None) -> list:
    """Ranker v1 scores features; 'rrf' keeps the raw fusion order for comparison."""
    if ranker == "rrf":
        ordered = sorted(merged, key=lambda h: (-h.score, h.best_rank))
        return [ranking.Ranked(hit=h, score=h.score) for h in ordered]
    return ranking.rank(merged, query, reputation=reps)


# ------------------------------------------------------------------- routes
def _safe_next(raw: str) -> str:
    """Only same-site paths, as an allowlist.

    Denying "//" and backslashes was one character away from being wrong; parsing
    the value and rejecting anything with a scheme or netloc is unambiguous.
    """
    if not raw.startswith("/") or raw.startswith("//"):
        return "/"
    # browsers normalise a backslash to "/", so "/\evil.com" is "//evil.com"
    if "\\" in raw:
        return "/"
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw):
        return "/"
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return "/"
    return raw


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = Query("/")) -> HTMLResponse:
    if auth.verify_token(_session(request)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return HTMLResponse(render.login(_safe_next(next)))


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    form = await request.form()
    nxt = _safe_next(str(form.get("next", "/")))
    password = str(form.get("password", ""))
    user = str(form.get("user", AUTH_USER)) or AUTH_USER
    ip = _client_ip(request)

    if auth.blocked(ip):
        return HTMLResponse(render.login(nxt, "Too many attempts. Wait 15 minutes."),
                            status_code=429)
    if not (hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(password, AUTH_PASS)):
        auth.record_failure(ip)
        return HTMLResponse(render.login(nxt, "Wrong password."), status_code=401)

    auth.clear_failures(ip)
    token = auth.issue_token(AUTH_USER)
    if not token:
        return HTMLResponse(
            render.login(nxt, "Cannot sign a session: no session secret is configured."),
            status_code=500)
    resp = RedirectResponse(nxt, status_code=303)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.session_days() * 86400,
                    httponly=True, samesite="lax", secure=(proto == "https"),
                    path="/")
    return resp


@app.get("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    resp.delete_cookie("ms_session", path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    chosen = set(_pick_engines(request, None))
    return HTMLResponse(render.home([s for s in ENGINES if s.name in chosen],
                                    ENGINES, _cookie(request, "theme")))


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query("", max_length=512, description="query"),
    page: int = Query(1, ge=1, le=20),
    time: str | None = Query(None),
    ss: str | None = Query(None),
    per: int | None = Query(None),
    engines: str | None = Query(None),
    nocache: int = Query(0),
    rank: str = Query("v1"),
) -> HTMLResponse:
    q = q.strip()
    theme = _cookie(request, "theme")
    if q and not _rate_ok(_session(request) or _client_ip(request)):
        return HTMLResponse(render.rate_limited(theme), status_code=429,
                            headers={"Retry-After": "60"})
    chosen = _pick_engines(request, engines)
    ss_cur, per_cur, time_range = _prefs(request, ss, per, time)

    if not q:
        return HTMLResponse(render.home([s for s in ENGINES if s.name in chosen],
                                        ENGINES, theme))

    bang = bangs.resolve(q)
    if bang:
        return RedirectResponse(bang["url"], status_code=302)

    data = await search(q, chosen, page, per_cur, ss_cur, time_range,
                        use_cache=not nocache, ranker=rank,
                        deep_policy=_cookie(request, "deep", "auto"))
    impression_id = feedback.new_impression_id()
    if data["window"]:
        # log what is actually shown: a click without an impression cannot be
        # evaluated later, and position bias lives in the impression
        feedback.log_impression(impression_id, q, rank, chosen,
                                data["ranked"][(page - 1) * per_cur: page * per_cur],
                                start_position=(page - 1) * per_cur + 1)
    return HTMLResponse(render.results_page(
        q=q, hits=data["window"], page=page, per=per_cur,
        total_unique=len(data["merged"]), elapsed=data["elapsed"],
        results=data["results"], engines_on=set(chosen), all_engines=ENGINES,
        time_range=time_range, ss=ss_cur, theme=theme,
        cached_age=data["cached_age"], impression_id=impression_id,
        reps=data.get("reps"), new_tab=_cookie(request, "newtab") == "1"))


@app.get("/preferences", response_class=HTMLResponse)
async def preferences(request: Request) -> Response:
    params = request.query_params
    save, q = params.get("save", ""), params.get("q", "")
    theme, ss = params.get("theme", ""), params.get("ss", "moderate")
    try:
        per = int(params.get("per", "10"))
    except ValueError:
        per = 10
    # the form posts one checkbox value per engine; filter links use a comma list
    raw_engines = params.getlist("engines")
    if len(raw_engines) == 1 and "," in raw_engines[0]:
        raw_engines = raw_engines[0].split(",")
    if save:
        valid = {s.name for s in ENGINES}
        chosen = [e for e in raw_engines if e in valid]
        target = f"/search?{urlencode({'q': q})}" if q else "/"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie("tv_engines", ",".join(chosen), max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="lax")
        resp.set_cookie("tv_theme", theme if theme in ("light", "dark") else "",
                        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
        resp.set_cookie("tv_ss", ss if ss in ("off", "moderate", "strict") else "moderate",
                        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
        resp.set_cookie("tv_per", str(per if per in PER_CHOICES else 10),
                        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
        # a checkbox that is unchecked never reaches the query string
        resp.set_cookie("tv_newtab", "1" if params.get("newtab") else "0",
                        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
        # "auto" keeps a 6s deep engine off the critical path unless the fast tier
        # cannot fill a page; "always" is the old behaviour, for when you want it
        resp.set_cookie("tv_deep", "always" if params.get("deep") == "always" else "auto",
                        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
        return resp

    ss_cur, per_cur, _ = _prefs(request, None, None, None)
    return HTMLResponse(render.preferences(ENGINES, set(_pick_engines(request, None)),
                                           _cookie(request, "theme"),
                                           ss_cur, per_cur, q,
                                           _cookie(request, "newtab") == "1",
                                           _cookie(request, "deep", "auto")))


@app.get("/settings/keys", response_class=HTMLResponse)
async def keys_get(request: Request, saved: str | None = Query(None),
                   error: str | None = Query(None)) -> HTMLResponse:
    message = ""
    if saved:
        message = f"Saved {saved}. The engine is available now."
    elif error:
        message = error
    # every declared engine, not just the ones already keyed: this page is where
    # an unkeyed engine becomes available
    return HTMLResponse(render.keys_page(
        SPECS, secrets_store.status(), secrets_store.available(),
        _csrf(request), message, _cookie(request, "theme")))


@app.post("/settings/keys")
async def keys_post(request: Request) -> Response:
    if not (AUTH_USER and AUTH_PASS) and not _loopback(BIND_HOST):
        # no session exists to bind a CSRF token to, and anyone can reach this page
        return RedirectResponse(
            "/settings/keys?error=" + quote("Key changes require authentication."),
            status_code=303)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    expected = _csrf(request)
    if not expected or not hmac.compare_digest(form.get("csrf", ""), expected):
        return RedirectResponse("/settings/keys?error=" + quote("Bad or expired form token."),
                                status_code=303)
    key = form.get("key", "")
    valid = {name for spec in SPECS for name in spec.env}
    if key not in valid:
        return RedirectResponse("/settings/keys?error=" + quote("Unknown key name."),
                                status_code=303)
    action = form.get("action", "save")
    try:
        if action == "delete":
            secrets_store.delete(key)
            _reload_engines()
            return RedirectResponse("/settings/keys?saved=" + quote(key + " removed"),
                                    status_code=303)
        value = form.get("value", "").strip()
        if not value:
            return RedirectResponse("/settings/keys", status_code=303)
        secrets_store.save(key, value)
    except secrets_store.NoSecret as exc:
        return RedirectResponse("/settings/keys?error=" + quote(str(exc)), status_code=303)
    except ValueError as exc:
        return RedirectResponse("/settings/keys?error=" + quote(str(exc)), status_code=303)
    _reload_engines()
    return RedirectResponse("/settings/keys?saved=" + quote(key), status_code=303)


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(hours: int = Query(24)) -> HTMLResponse:
    hours = 168 if hours > 24 else 24
    return HTMLResponse(render.stats_page(
        stats.summary(hours), hours, {s.name: s.label for s in ENGINES}))


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request) -> HTMLResponse:
    return HTMLResponse(render.about(ENGINES, _cookie(request, "theme")))


@app.get("/opensearch.xml")
async def opensearch(request: Request) -> Response:
    """The host comes from the request, so it must be validated and escaped: a
    forged Host header used to be reflected straight into the XML."""
    base = os.environ.get("TRIVET_BASE_URL", "").rstrip("/")
    if not base:
        parts = urlsplit(str(request.base_url))
        host = (parts.hostname or "").lower()
        allowed = {h.strip().lower() for h in
                   os.environ.get("TRIVET_HOSTS", "").split(",") if h.strip()}
        if allowed and host not in allowed:
            return Response("unknown host", status_code=400, media_type="text/plain")
        if not host or not all(c.isalnum() or c in ".-:" for c in host):
            return Response("bad host", status_code=400, media_type="text/plain")
        base = f"{parts.scheme}://{parts.netloc}"
    base = xml_escape(base)
    return Response(render.OPENSEARCH.format(base=base),
                    media_type="application/opensearchdescription+xml")


@app.get("/favicon.svg")
async def favicon() -> Response:
    return Response(render.FAVICON, media_type="image/svg+xml")


@app.get("/debug/rank", response_class=HTMLResponse)
async def debug_rank(request: Request, q: str = Query("", max_length=512),
                     engines: str | None = Query(None),
                     nocache: int = Query(0)) -> HTMLResponse:
    if not q.strip():
        return HTMLResponse("<p style='font-family:system-ui;padding:2rem'>"
                            "Add ?q=your+query</p>")
    chosen = _pick_engines(request, engines)
    # same depth as the real results view, so the table explains what the user sees
    data = await search(q.strip(), chosen, 1, 10, "moderate", "any",
                        use_cache=not nocache, ranker="v1")
    rrf = await search(q.strip(), chosen, 1, 10, "moderate", "any",
                       use_cache=True, ranker="rrf")
    return HTMLResponse(render.rank_debug(q.strip(), data["ranked"][:20],
                                          rrf["ranked"][:20],
                                          ranking.load_config(),
                                          data["cached_age"] is not None,
                                          data.get("reps") or {}))


@app.post("/api/click")
async def api_click(request: Request) -> JSONResponse:
    """Beacon target: records which impression position the user opened."""
    try:
        body = await request.json()
        iid = str(body.get("impression_id", ""))[:32]
        pos = int(body.get("position", 0))
        url = str(body.get("url", ""))[:2048]
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)
    if not iid or pos < 1 or not url.startswith("http"):
        return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)
    return JSONResponse({"ok": feedback.log_click(iid, pos, url)})


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "version": __version__,
            "engines": [s.name for s in ENGINES], "cache": cache.stats(),
            "datasets": {"smallweb": smallweb.size(), "bangs": bangs.size()}}


@app.get("/api/search")
async def api_search(request: Request, q: str = Query(..., min_length=1, max_length=512),
                     page: int = Query(1, ge=1, le=20),
                     limit: int = Query(10, ge=1, le=50),
                     time: str | None = None, ss: str | None = None,
                     engines: str | None = None,
                     nocache: int = Query(0),
                     rank: str = Query("v1"),
                     explain: int = Query(0)) -> JSONResponse:
    if not _rate_ok(_session(request) or _client_ip(request)):
        return JSONResponse({"ok": False, "error": "rate limited"},
                            status_code=429, headers={"Retry-After": "60"})
    bang = bangs.resolve(q)
    if bang:
        return JSONResponse({"query": q, "bang": bang, "results": [],
                             "took_ms": 0, "unique": 0, "raw": 0})
    chosen = _pick_engines(request, engines)
    ss_cur, _, time_range = _prefs(request, ss, None, time)
    data = await search(q, chosen, page, limit, ss_cur, time_range,
                        use_cache=not nocache, ranker=rank,
                        deep_policy=_cookie(request, "deep", "auto"))
    return JSONResponse({
        "query": q,
        "took_ms": int(data["elapsed"] * 1000),
        "cached_age_s": round(data["cached_age"], 1) if data["cached_age"] is not None else None,
        "unique": len(data["merged"]),
        "raw": sum(len(r.hits) for r in data["results"] if r.ok),
        "consensus": engine_consensus(data["results"]),
        "engines": [{"name": r.name, "ok": r.ok, "count": len(r.hits), "ms": r.ms,
                     "error": r.error, "cost_usd": r.cost_usd,
                     "skipped": r.skipped} for r in data["results"]],
        "results": [{"title": h.title, "url": h.url, "snippet": h.snippet,
                     "engines": h.engines, "agreement": h.agreement,
                     "score": round(h.score, 5), "published": h.published}
                    for h in data["window"]],
        "ranker": rank,
        "ranked_all": [
            {"rank": i, "title": r.hit.title, "url": r.hit.url,
             "score": round(r.score, 5),
             **({"features": r.features.as_dict()} if explain else {})}
            for i, r in enumerate(data["ranked"], start=1)],
    })


def cli(query: str, page: int, per: int, time_range: str) -> None:
    selected = [s.name for s in ENGINES]
    data = asyncio.run(search(query, selected, page, per, "moderate", time_range))
    print(f'\n"{query}"  {data["elapsed"]:.2f}s  {len(data["merged"])} merged from '
          f'{sum(len(r.hits) for r in data["results"] if r.ok)} raw')
    for r in data["results"]:
        state = f"{len(r.hits)} results {r.ms}ms" if r.ok else f"FAIL {r.error}"
        print(f"  {r.name:<10} {state}")
    print()
    for i, h in enumerate(data["merged"][:per], start=1):
        print(f"{i:>2}. {h.title}")
        print(f"    {h.url}")
        print(f"    [{', '.join(h.engines)}]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--cli", action="store_true")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--per", type=int, default=10)
    ap.add_argument("--time", default="any")
    ap.add_argument("--port", type=int, default=8952)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.query:
        cli(args.query, args.page, args.per, args.time)
    else:
        globals()["BIND_HOST"] = args.host
        _check_auth_config(args.host)     # refuse to start open on a public interface
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
