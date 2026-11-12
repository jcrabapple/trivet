"""Smoke tests for the parts where a regression is a security problem.

Run with `python3 -m pytest -q`. No network: every engine call is stubbed, and the
tests that touch SQLite use a temporary database.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate every test from the developer's real config and database."""
    monkeypatch.setenv("TRIVET_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("TRIVET_SECRET_KEY", "test-secret-key-not-real")
    monkeypatch.setenv("TRIVET_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("TRIVET_USER", "admin")
    monkeypatch.setenv("TRIVET_PASS", "hunter2")
    for name in ("KEENABLE_API_KEY", "EXA_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY",
                 "SERPER_API_KEY", "LINKUP_API_KEY", "FIRECRAWL_API_KEY", "VALYU_API_KEY",
                 "PARALLEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _fresh(module: str):
    if module in sys.modules:
        del sys.modules[module]
    return importlib.import_module(module)


# --------------------------------------------------------------- startup guards
def test_half_configured_credentials_refuse_to_start(env):
    env.delenv("TRIVET_PASS")
    app = _fresh("app")
    with pytest.raises(SystemExit):
        app._check_auth_config("127.0.0.1")


def test_public_bind_without_credentials_refuses(env):
    env.delenv("TRIVET_USER")
    env.delenv("TRIVET_PASS")
    app = _fresh("app")
    with pytest.raises(SystemExit):
        app._check_auth_config("0.0.0.0")


def test_loopback_without_credentials_is_allowed(env):
    env.delenv("TRIVET_USER")
    env.delenv("TRIVET_PASS")
    app = _fresh("app")
    app._check_auth_config("127.0.0.1")      # no exception


# ------------------------------------------------------------------ url handling
def test_safe_href_rejects_non_http_schemes(env):
    render = _fresh("render")
    assert render._safe_href("javascript:alert(1)") == "#"
    assert render._safe_href("JaVaScRiPt:alert(1)") == "#"
    assert render._safe_href("data:text/html,x") == "#"
    assert render._safe_href("https://example.com/x") == "https://example.com/x"


def test_engine_payload_drops_unsafe_urls(env):
    providers = _fresh("providers")
    hits = providers._hits([
        {"url": "javascript:alert(1)", "title": "bad"},
        {"url": "data:text/html,x", "title": "bad"},
        {"url": "https://example.com/ok", "title": "good"},
    ], "test")
    assert [h.url for h in hits] == ["https://example.com/ok"]


def test_next_parameter_must_stay_on_site(env):
    app = _fresh("app")
    assert app._safe_next("/search?q=x") == "/search?q=x"
    assert app._safe_next("//evil.com") == "/"
    assert app._safe_next("https://evil.com") == "/"
    assert app._safe_next("/\\evil.com") == "/"
    assert app._safe_next("/path\twith-tab") == "/"


# ----------------------------------------------------------------- key handling
def test_stored_keys_round_trip_and_hide_the_value(env):
    store = _fresh("secrets_store")
    store.save("EXA_API_KEY", "exa-abcdef123456")
    assert store.load()["EXA_API_KEY"] == "exa-abcdef123456"
    status = store.status()["EXA_API_KEY"]
    assert status["last4"] == "3456"
    assert "abcdef" not in str(status)          # the page may only ever see last4


def test_key_ciphertext_is_not_plaintext_on_disk(env):
    store = _fresh("secrets_store")
    store.save("EXA_API_KEY", "exa-abcdef123456")
    import sqlite3
    conn = sqlite3.connect(os.environ["TRIVET_DB"])
    ct = conn.execute("SELECT ciphertext FROM api_keys").fetchone()[0]
    assert b"abcdef" not in bytes(ct)


def test_storing_refuses_without_a_secret(env):
    env.delenv("TRIVET_SECRET_KEY")
    env.delenv("TRIVET_SESSION_SECRET")
    store = _fresh("secrets_store")
    with pytest.raises(store.NoSecret):
        store.save("EXA_API_KEY", "exa-abcdef123456")


def test_keys_are_bound_to_their_name(env):
    """A ciphertext row cannot be moved to another key name."""
    store = _fresh("secrets_store")
    store.save("EXA_API_KEY", "exa-abcdef123456")
    import sqlite3
    conn = sqlite3.connect(os.environ["TRIVET_DB"])
    nonce, ct = conn.execute(
        "SELECT nonce, ciphertext FROM api_keys WHERE name='EXA_API_KEY'").fetchone()
    conn.execute("UPDATE api_keys SET nonce=?, ciphertext=? WHERE name='EXA_API_KEY'",
                 (nonce, ct))
    conn.execute("INSERT INTO api_keys (name, nonce, ciphertext, last4, updated_at) "
                 "VALUES ('BRAVE_API_KEY', ?, ?, '3456', 0)", (nonce, ct))
    conn.commit()
    loaded = store.load()
    assert "EXA_API_KEY" in loaded
    assert "BRAVE_API_KEY" not in loaded       # associated data mismatch


# ------------------------------------------------------------------ cache keys
def test_cache_key_includes_filters_and_policy(env):
    cache = _fresh("cache")
    base = cache.make_key("q", ["exa"], "any", "moderate", 10, "auto")
    assert base != cache.make_key("q", ["exa"], "week", "moderate", 10, "auto")
    assert base != cache.make_key("q", ["exa"], "any", "strict", 10, "auto")
    assert base != cache.make_key("q", ["exa", "brave"], "any", "moderate", 10, "auto")
    assert base != cache.make_key("q", ["exa"], "any", "moderate", 20, "auto")
    assert base == cache.make_key("  q  ", ["exa"], "any", "moderate", 10, "auto")


def test_deep_policy_changes_the_key(env):
    """An 'always' request must not be served a page that skipped the deep engine."""
    cache = _fresh("cache")
    assert (cache.make_key("q", ["exa", "valyu"], "any", "moderate", 10, "always")
            != cache.make_key("q", ["exa", "valyu"], "any", "moderate", 10, "auto"))


# ---------------------------------------------------------------- session tokens
def test_session_token_expiry_and_epoch(env):
    auth = _fresh("auth")
    token = auth.issue_token("admin")
    assert auth.verify_token(token) == "admin"
    env.setenv("TRIVET_SESSION_EPOCH", str(int(__import__("time").time()) + 60))
    assert auth.verify_token(token) is None      # epoch evicts older sessions
    env.delenv("TRIVET_SESSION_EPOCH")
    assert auth.verify_token(token + "x") is None


def test_session_secret_is_generated_and_persisted(env, tmp_path):
    env.delenv("TRIVET_SESSION_SECRET")
    env.setenv("TRIVET_SESSION_SECRET_FILE", str(tmp_path / "secret"))
    auth = _fresh("auth")
    first = auth.session_secret()
    assert first and len(first) > 20
    assert (tmp_path / "secret").stat().st_mode & 0o077 == 0
    assert auth.session_secret() == first        # stable across calls, survives restart


# ------------------------------------------------------------- report an issue
def _hit(i, title_len=80, url_len=60):
    from merge import MergedHit
    return MergedHit(url="https://example.com/" + "u" * url_len + str(i),
                     title="T" * title_len + str(i), engines=["exa"], score=0.4,
                     best_rank=1)


class _R:
    def __init__(self, name, ok=True, ms=100):
        self.name, self.ok, self.ms, self.error = name, ok, ms, None


def test_report_url_carries_query_and_results(env):
    import urllib.parse
    render = _fresh("render")
    hits = [_hit(i) for i in range(10)]
    url = render._issue_url("best android browser", hits, page=1, per=10,
                            results=[_R("exa")], engines_on={"exa"},
                            time_range="any", ss="moderate", labels={"exa": "Exa"})
    assert url.startswith("https://github.com/")
    assert "/issues/new?" in url
    assert len(url) <= render.ISSUE_URL_LIMIT
    body = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["body"][0]
    assert "best android browser" in body
    assert body.count('"pos"') == 10
    assert "Exa" in body            # engine labels, not raw names


def test_report_url_shrinks_to_fit(env, monkeypatch):
    """A long query must still produce a usable link, not a broken one."""
    import urllib.parse
    render = _fresh("render")
    monkeypatch.setattr(render, "ISSUE_URL_LIMIT", 1600)
    hits = [_hit(i, 200, 200) for i in range(10)]
    url = render._issue_url("x" * 200, hits, page=1, per=10, results=[_R("exa")],
                            engines_on={"exa"}, time_range="any", ss="moderate")
    assert len(url) <= 1600
    body = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["body"][0]
    assert "**Query:**" in body
    assert "cut to" in body
    assert body.count('"pos"') < 10


def test_report_url_honours_repo_override(env):
    render = _fresh("render")
    url = render._issue_url("q", [_hit(0)], page=1, per=10, results=[_R("exa")],
                            engines_on={"exa"}, time_range="any", ss="moderate",
                            repo="someone/fork")
    assert "github.com/someone/fork/issues/new" in url


# ------------------------------------------------------------------- rate limit
def test_rate_limit_trips_and_recovers(env):
    app = _fresh("app")
    app._RECENT.clear()
    for _ in range(app.RATE_LIMIT):
        assert app._rate_ok("bucket")
    assert not app._rate_ok("bucket")
    assert app._rate_ok("other-bucket")


# --------------------------------------------------- image search & thumb proxy
def test_cache_key_separates_web_and_images(env):
    cache = _fresh("cache")
    web_k = cache.make_key("red panda", ["brave", "serper"], "any", "moderate", 24, "auto", "web")
    img_k = cache.make_key("red panda", ["brave", "serper"], "any", "moderate", 24, "auto", "images")
    assert web_k != img_k


def test_thumb_proxy_signatures_are_bound_to_url_and_secret(env):
    tp = _fresh("thumb_proxy")
    url = "https://imgs.search.brave.com/example/thumb.jpg"
    sig = tp.sign_url(url)
    assert sig and tp.verify_sig(url, sig)
    assert not tp.verify_sig(url + "?x=1", sig)
    assert not tp.verify_sig(url, sig[:-1] + ("0" if sig[-1] != "0" else "1"))
    assert tp.sign_url("javascript:alert(1)") == ""
    assert tp.sign_url("file:///etc/passwd") == ""

    env.setenv("TRIVET_SESSION_SECRET", "rotated-session-secret")
    assert not tp.verify_sig(url, sig)


def test_thumb_proxy_blocks_non_public_ips_and_svg(env):
    import asyncio
    tp = _fresh("thumb_proxy")
    for blocked in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "10.0.0.8",
                    "172.16.5.4", "192.168.1.1", "100.64.0.1", "169.254.1.1",
                    "0.0.0.0"):
        assert not tp.is_public_ip(blocked), blocked
    assert tp.is_public_ip("93.184.216.34")

    with pytest.raises(tp.DisallowedTarget):
        asyncio.run(tp._ensure_public_target("http://127.0.0.1:8952/healthz"))

    # SVG and HTML must be rejected even if upstream lies about Content-Type.
    assert tp.detect_raster_type("image/svg+xml", b"<svg onload=alert(1)>") is None
    assert tp.detect_raster_type("image/png", b"<svg onload=alert(1)>") is None
    assert tp.detect_raster_type("image/jpeg", b"<!doctype html><html>") is None
    assert tp.detect_raster_type("application/octet-stream", b"\xff\xd8\xff\xe0jpeg") == "image/jpeg"
    assert tp.detect_raster_type("image/png", b"\x89PNG\r\n\x1a\nxxxx") == "image/png"


def test_thumb_proxy_blocks_redirect_to_loopback(env):
    import asyncio
    import httpx

    tp = _fresh("thumb_proxy")
    tp._LRU.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:8952/healthz"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Stub DNS for the first hop so the test stays offline; hop 2 hits literal loopback.
    async def run():
        orig = tp._ensure_public_target

        async def fake_check(u: str):
            if "public.example" in u:
                return
            await orig(u)

        tp._ensure_public_target = fake_check
        try:
            with pytest.raises(tp.DisallowedTarget):
                await tp.fetch_thumbnail("https://public.example/pic.jpg", client=client)
        finally:
            tp._ensure_public_target = orig
            await client.aclose()

    asyncio.run(run())


def test_image_merge_dedupes_on_image_url_and_upgrades_thumbnail(env):
    providers = _fresh("providers")
    merge_mod = _fresh("merge")
    r_firecrawl = providers.EngineResult("firecrawl", ok=True, hits=[
        providers.Hit(title="Red Panda", url="https://zoo.example/gallery",
                      image_url="https://zoo.example/img/1.jpg",
                      thumb_url="https://zoo.example/img/1.jpg", engine="firecrawl", rank=1),
        providers.Hit(title="Red Panda Two", url="https://zoo.example/gallery",
                      image_url="https://zoo.example/img/2.jpg",
                      thumb_url="https://zoo.example/img/2.jpg", engine="firecrawl", rank=2),
    ])
    r_serper = providers.EngineResult("serper", ok=True, hits=[
        providers.Hit(title="Red Panda", url="https://mirror.example/post",
                      image_url="https://zoo.example/img/1.jpg?utm_source=x",
                      thumb_url="https://encrypted-tbn0.gstatic.com/images?q=tbn:1",
                      width=1600, height=1200, engine="serper", rank=1),
    ])
    merged = merge_mod.merge([r_firecrawl, r_serper])
    assert len(merged) == 2                  # gallery page kept both distinct images
    top = merged[0]
    assert top.agreement == 2                # same image fused across engines
    assert "gstatic.com" in top.thumb_url    # upgraded from origin image to CDN thumbnail
    assert (top.width, top.height) == (1600, 1200)


def test_image_results_page_uses_same_origin_proxy_urls(env):
    providers = _fresh("providers")
    merge_mod = _fresh("merge")
    render = _fresh("render")
    hit = merge_mod.MergedHit(
        title="Red Panda",
        url="https://safeworldwide.org/red-panda/",
        image_url="https://safeworldwide.org/panda.jpg",
        thumb_url="https://imgs.search.brave.com/thumb.jpg",
        width=1200, height=800,
        engines=["brave", "serper"], score=0.032, best_rank=1,
    )
    specs = [s for s in providers.SPECS if s.image_fn is not None]
    html = render.results_page(
        q="red panda", hits=[hit], page=1, per=24, total_unique=1, elapsed=0.42,
        results=[_R("brave"), _R("serper")], engines_on={"brave", "serper"},
        all_engines=specs, time_range="any", ss="moderate", mode="images",
    )
    assert 'src="/img?u=https%3A%2F%2Fimgs.search.brave.com%2Fthumb.jpg&amp;sig=' in html
    assert 'src="https://' not in html       # strict CSP: no direct remote <img src>
    assert "1 image" in html                 # singular grammar check
    assert "1200×800" in html
    assert 'href="/search?q=red+panda"' in html   # Web tab drops per-tab engine narrowing


def test_img_route_requires_auth_and_valid_hmac(env, monkeypatch):
    import base64
    from fastapi.testclient import TestClient

    tp = _fresh("thumb_proxy")
    app_mod = _fresh("app")
    client = TestClient(app_mod.app)
    target = "https://imgs.search.brave.com/sample.jpg"
    sig = tp.sign_url(target)

    # Unauthenticated request redirects to login.
    unauth = client.get(f"/img?u={target}&sig={sig}", follow_redirects=False)
    assert unauth.status_code == 303

    hdrs = {"Authorization": "Basic " + base64.b64encode(b"admin:hunter2").decode()}
    bad = client.get(f"/img?u={target}&sig={'0' * 24}", headers=hdrs)
    assert bad.status_code == 403

    async def fake_fetch(url, client=None):
        return b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg"

    monkeypatch.setattr(app_mod.thumb_proxy, "fetch_thumbnail", fake_fetch)
    good = client.get(f"/img?u={target}&sig={sig}", headers=hdrs)
    assert good.status_code == 200
    assert good.headers["content-type"] == "image/jpeg"
    assert "sandbox" in good.headers["content-security-policy"]


# ------------------------------------------------------------------ news search
def test_news_date_parsing_handles_all_engine_formats(env):
    from datetime import datetime, timezone
    providers = _fresh("providers")
    ref = datetime(2026, 11, 11, 6, 0, 0, tzinfo=timezone.utc).timestamp()

    # Exa ISO with Z
    exa_h = providers.parse_age_hours("2026-11-10T18:00:00.000Z", now=ref)
    assert exa_h == pytest.approx(12.0)
    assert providers.fmt_news_age("2026-11-10T18:00:00.000Z", now=ref) == "12h ago"

    # Brave naive ISO page_age
    brave_h = providers.parse_age_hours("2026-11-10T12:00:00", now=ref)
    assert brave_h == pytest.approx(18.0)

    # Tavily RFC-2822
    tav_h = providers.parse_age_hours("Tue, 10 Nov 2026 06:00:00 GMT", now=ref)
    assert tav_h == pytest.approx(24.0)

    # Serper / Firecrawl relative strings
    assert providers.parse_age_hours("17 hours ago", now=ref) == pytest.approx(17.0)
    assert providers.fmt_news_age("45 minutes ago", now=ref) == "45m ago"
    assert providers.fmt_news_age("5 days ago", now=ref) == "5d ago"
    assert providers.fmt_news_age("2026-08-16T23:02:32.000Z", now=ref) == "16 Aug 2026"

    # Future clock skew is clamped to 0 rather than going negative.
    assert providers.parse_age_hours("2026-11-11T08:00:00Z", now=ref) == 0.0


def test_news_ranker_prefers_fresh_stories_and_consensus(env):
    merge_mod = _fresh("merge")
    app_mod = _fresh("app")

    stale_exa = merge_mod.MergedHit(
        title="Kernel 7.2 Released", url="https://9to5linux.com/lk72",
        engines=["exa"], score=1 / 61, best_rank=1,
        published="2026-08-16T23:02:32.000Z",
    )
    fresh_serper = merge_mod.MergedHit(
        title="Breaking Kernel Fix", url="https://phoronix.com/news/fix",
        engines=["serper"], score=1 / 61, best_rank=1,
        published="6 hours ago", source_name="Phoronix",
    )
    consensus_recent = merge_mod.MergedHit(
        title="Major Maintainer Update", url="https://lwn.net/Articles/1",
        engines=["brave", "serper"], score=2 / 62, best_rank=2,
        published="2 days ago", source_name="LWN.net",
    )
    ranked = app_mod._order_news([stale_exa, fresh_serper, consensus_recent])
    urls = [r.hit.url for r in ranked]
    assert urls[0] == "https://lwn.net/Articles/1"
    assert urls[1] == "https://phoronix.com/news/fix"
    assert urls[2] == "https://9to5linux.com/lk72"


def test_strip_leading_title_cleans_duplicated_headlines(env):
    providers = _fresh("providers")
    title = "Longtime Linux Developer Steps Down From Some Roles"
    raw = "Longtime Linux Developer Steps Down From Some Roles Written by Michael Larabel"
    assert providers._strip_leading_title(raw, title) == "Written by Michael Larabel"


def test_news_results_page_is_text_only_with_outlet_and_tabs(env):
    providers = _fresh("providers")
    merge_mod = _fresh("merge")
    render = _fresh("render")
    hit = merge_mod.MergedHit(
        title="Longtime Linux Developer Steps Down",
        url="https://www.phoronix.com/news/example",
        snippet="Written by Michael Larabel in Linux Kernel.",
        engines=["brave", "serper"], score=0.032, best_rank=1,
        published="5 hours ago", source_name="Phoronix",
    )
    specs = [s for s in providers.SPECS if s.news_fn is not None]
    html = render.results_page(
        q="linux kernel", hits=[hit], page=1, per=10, total_unique=1, elapsed=0.88,
        results=[_R("brave"), _R("serper")], engines_on={"brave", "serper"},
        all_engines=specs, time_range="any", ss="moderate", mode="news",
    )
    assert "<img " not in html                   # text-only: no thumbnails rendered
    assert '<span class="outlet">Phoronix</span>' in html
    assert "phoronix.com" in html
    assert "5h ago" in html
    assert "1 article" in html
    assert 'name="tab" value="news"' in html
    assert 'aria-current="page" href="/search?q=linux+kernel&amp;tab=news">News</a>' in html


def test_news_cache_ttl_expires_before_web_ttl(env):
    import sqlite3
    import time
    cache = _fresh("cache")
    k = cache.make_key("linux", ["brave"], "any", "moderate", 10, "auto", "news")
    cache.put(k, "linux", {"v": 4, "merged": [], "engines": []})
    with sqlite3.connect(cache.CACHE_PATH) as conn:
        conn.execute("UPDATE queries SET created_at = ? WHERE key = ?",
                     (time.time() - 420, k))  # 7 minutes old
    assert cache.get(k, ttl=cache.NEWS_TTL_SECONDS) is None   # > 5m news TTL
    assert cache.get(k) is not None                           # < 15m web TTL


# --------------------------------------------------------------- web ranker v2
def test_clean_preserves_snake_case_and_unwraps_google_redirects(env):
    providers = _fresh("providers")
    assert "create_task" in providers._clean("Use **asyncio.create_task()** and _italic_ text")
    assert "client_max_body_size" in providers._clean("Set `client_max_body_size` in nginx")

    hits = providers._hits([
        {"url": "https://www.google.com/url?opi=1&q=CAES_opaque_blob", "title": "junk"},
        {"url": "https://www.google.com/url?q=https://example.org/guide", "title": "real"},
    ], "serper")
    assert [h.url for h in hits] == ["https://example.org/guide"]


def test_gov_edu_suffixes_match_and_personal_edu_mirrors_are_discounted(env):
    ranking = _fresh("ranking")
    terms = ranking.query_terms("rust ownership and borrowing")
    nps, _, _ = ranking._domain_prior("nps.gov", "https://www.nps.gov/shen/index.htm",
                                      query="shenandoah national park")
    pmc, _, _ = ranking._domain_prior("pmc.ncbi.nlm.nih.gov",
                                      "https://pmc.ncbi.nlm.nih.gov/articles/PMC4421632",
                                      query="creatine kidneys")
    rust_doc, _, _ = ranking._domain_prior("doc.rust-lang.org",
                                           "https://doc.rust-lang.org/book/ch04-00.html",
                                           query="rust ownership and borrowing", terms=terms)
    mit_mirror, _, _ = ranking._domain_prior("web.mit.edu",
                                             "https://web.mit.edu/rust-lang_v1.25/book.html",
                                             query="rust ownership and borrowing", terms=terms)
    assert nps >= 0.80
    assert pmc >= 0.80          # numeric PMC ID must not subtract from .gov prior
    assert rust_doc > mit_mirror


def test_subject_docs_outrank_qa_and_random_github_blobs(env):
    merge_mod = _fresh("merge")
    ranking = _fresh("ranking")
    q = "fastapi background tasks"
    official = merge_mod.MergedHit(
        title="Background Tasks - FastAPI",
        url="https://fastapi.tiangolo.com/tutorial/background-tasks",
        snippet="Create background tasks to be run after returning a response.",
        engines=["keenable", "exa", "brave", "serper"], score=0.065, best_rank=1,
        sources=[{"e": "keenable", "r": 1}, {"e": "exa", "r": 1},
                 {"e": "brave", "r": 1}, {"e": "serper", "r": 1}],
    )
    so_post = merge_mod.MergedHit(
        title="FastAPI asynchronous background tasks blocks other requests?",
        url="https://stackoverflow.com/questions/67599119/fastapi-background-tasks",
        snippet="Why do FastAPI background tasks block?",
        engines=["keenable", "exa", "brave", "serper"], score=0.062, best_rank=2,
        sources=[{"e": "keenable", "r": 2}, {"e": "exa", "r": 2},
                 {"e": "brave", "r": 3}, {"e": "serper", "r": 2}],
    )
    gh_blob = merge_mod.MergedHit(
        title="9-fastapi-background-tasks/index.mdx",
        url="https://github.com/someone/tutorials/blob/main/docs/fastapi-background-tasks.mdx",
        snippet="FastAPI background tasks tutorial.",
        engines=["exa"], score=0.015, best_rank=5,
        sources=[{"e": "exa", "r": 5}],
    )
    ordered = [r.hit.url for r in ranking.rank([so_post, gh_blob, official], q)]
    assert ordered == [official.url, so_post.url, gh_blob.url]


def test_vendor_blogs_are_topic_gated_and_short_tokens_require_word_boundaries(env):
    ranking = _fresh("ranking")
    # 'ai' must not match 'said', 'domain', or 'contain'
    terms = ranking.query_terms("ai code")
    assert ranking._coverage(terms, "He said the main domain may contain data") == 0.0
    # symmetric plural matching: 'kidneys' matches 'kidney'
    assert ranking._coverage(["kidneys"], "creatine monohydrate and kidney function") == 1.0

    # Cloudflare blog gets a strong boost on Cloudflare queries, modest on unrelated AI queries
    cf_on, _, _ = ranking._domain_prior("blog.cloudflare.com",
                                        "https://blog.cloudflare.com/cloudflare-tunnel",
                                        query="cloudflare tunnel ingress")
    cf_off, _, _ = ranking._domain_prior("blog.cloudflare.com",
                                         "https://blog.cloudflare.com/ai-code-review",
                                         query="ai code review tools")
    assert cf_on > cf_off + 0.40

    # Exact-match SEO domains on generic words must not get a brand-ownership boost
    seo_vpn, _, _ = ranking._domain_prior("thebestvpn.com",
                                          "https://thebestvpn.com/best-vpns-android/",
                                          query="best vpn for android",
                                          terms=ranking.query_terms("best vpn for android"),
                                          intent="tech", choice=True)
    assert seo_vpn == 0.0


def test_exact_entity_projects_outrank_alternate_spelling_reference_pages(env):
    merge_mod = _fresh("merge")
    ranking = _fresh("ranking")
    terms = ranking.query_terms("kyllene")

    exact_repo, _, _ = ranking._domain_prior(
        "github.com", "https://github.com/savy-91/kyllene", terms=terms, agreement=1)
    owner_only, _, _ = ranking._domain_prior(
        "github.com", "https://github.com/kyllene/herald", terms=terms, agreement=1)
    owner_consensus, _, _ = ranking._domain_prior(
        "github.com", "https://github.com/kyllene/herald", terms=terms, agreement=2)
    official, _, _ = ranking._domain_prior(
        "kyllene.app", "https://kyllene.app", terms=terms)
    spelling_match, _, _ = ranking._domain_prior(
        "en.wikipedia.org", "https://en.wikipedia.org/wiki/Kyllene", terms=terms)
    spelling_mismatch, _, _ = ranking._domain_prior(
        "en.wikipedia.org", "https://en.wikipedia.org/wiki/Cyllene_(nymph)", terms=terms)
    assert exact_repo == 1.0
    assert owner_only == 0.42
    assert owner_consensus == 0.62
    assert official == 0.72
    assert spelling_match == 0.80
    assert spelling_mismatch == 0.10

    def hit(title, url, score, rank, engines):
        return merge_mod.MergedHit(
            title=title, url=url, snippet="Kyllene is an unofficial Android client for Hermes Agent.",
            engines=engines, score=score, best_rank=rank,
            sources=[{"e": name, "r": rank} for name in engines])

    wiki = hit("Cyllene (nymph)", "https://en.wikipedia.org/wiki/Cyllene_(nymph)",
               0.06377, 1, ["keenable", "exa", "brave"])
    official_hit = hit("Kyllene: the Hermes Agent app for Android", "https://kyllene.app",
                       0.03227, 1, ["exa"])
    repo = hit("GitHub - savy-91/kyllene: Unofficial Android client for Hermes Agent.",
               "https://github.com/savy-91/kyllene", 0.01587, 3, ["serper"])
    redirected_repo = hit("GitHub - Giton22/kyllene: Unofficial Android client for Hermes Agent.",
                          "https://github.com/Giton22/kyllene", 0.01613, 2, ["serper"])
    ordered = ranking.rank([wiki, official_hit, repo, redirected_repo], "kyllene")
    assert ordered[0].hit.url == "https://kyllene.app"
    assert {r.hit.url for r in ordered[1:3]} == {repo.url, redirected_repo.url}
    assert ordered[3].hit.url == wiki.url


# ------------------------------------------------------------------ autosuggest
def test_serper_suggestions_are_clean_deduplicated_limited_and_cached(env):
    import asyncio
    import httpx
    suggest = _fresh("suggest")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert request.headers["X-API-KEY"] == "test-serper-key"
        return httpx.Response(200, json={"suggestions": [
            {"value": "fastapi back"},
            {"value": "  fastapi   background tasks  "},
            {"value": "fastapi background tasks"},
            {"value": "fastapi backend"},
            {"value": "fastapi background task vs celery"},
            {"value": "fastapi backend structure"},
            {"value": "fastapi async background"},
            {"value": "fastapi background worker"},
            {"value": "fastapi background asyncio"},
            {"value": "fastapi extra suggestion"},
            {"value": 42}, "junk",
        ]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await suggest.get(client, "  fastapi   back ", "test-serper-key")
            second = await suggest.get(client, "FASTAPI BACK", "test-serper-key")
            return first, second

    first, second = asyncio.run(exercise())
    assert first == second
    assert first[0] == "fastapi background tasks"
    assert len(first) == 8
    assert calls == 1                         # normalized, case-folded cache hit


def test_serper_suggestion_failures_are_not_cached(env):
    import asyncio
    import httpx
    suggest = _fresh("suggest")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"message": "temporary"})
        return httpx.Response(200, json={"suggestions": [{"value": "fastapi background"}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await suggest.get(client, "fastapi", "test-serper-key")
            return await suggest.get(client, "fastapi", "test-serper-key")

    assert asyncio.run(exercise()) == ["fastapi background"]
    assert calls == 2


def test_autosuggest_markup_is_progressive_and_preference_requires_serper(env):
    render = _fresh("render")
    enabled = render._search_form("fastapi", suggest=True)
    disabled = render._search_form("fastapi", suggest=False)
    assert 'data-suggest="1"' in enabled
    assert 'role="combobox"' in enabled
    assert 'aria-controls="query-suggestions"' in enabled
    assert 'role="listbox"' in enabled
    assert 'data-suggest=' not in disabled
    assert 'role="combobox"' not in disabled
    assert '<button type="submit" aria-label="Search">' in disabled

    no_key = render.preferences([], set(), "", "moderate", 10,
                                serper_available=False, suggest=True)
    keyed = render.preferences([], set(), "", "moderate", 10,
                               serper_available=True, suggest=True)
    assert 'name="suggest" value="1" disabled' in no_key
    assert "requires the Serper API key" in no_key
    assert 'name="suggest" value="1" checked' in keyed
    assert "Google (Serper)" in keyed


def test_suggest_route_enforces_key_preference_and_returns_json(env, monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    app_mod = _fresh("app")
    app_mod._RECENT.clear()
    client = TestClient(app_mod.app)
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:hunter2").decode()}

    app_mod.ENGINES = []
    assert client.get("/api/suggest?q=fa", headers=headers).status_code == 403

    app_mod.ENGINES = [app_mod.BY_NAME["serper"]]
    app_mod.ENV = {"SERPER_API_KEY": "test-serper-key"}

    async def fake_get(client, query, api_key):
        assert query == "fastapi back"
        assert api_key == "test-serper-key"
        return ["fastapi background tasks", "fastapi backend"]

    monkeypatch.setattr(app_mod.suggest, "get", fake_get)
    good = client.get("/api/suggest?q=fastapi%20back", headers=headers)
    assert good.status_code == 200
    assert good.json()["suggestions"] == ["fastapi background tasks", "fastapi backend"]
    assert good.headers["cache-control"] == "private, max-age=60"

    client.cookies.set("tv_suggest", "0")
    assert client.get("/api/suggest?q=fastapi", headers=headers).status_code == 403


def test_preference_save_cannot_enable_suggest_without_serper(env):
    import base64
    from fastapi.testclient import TestClient
    app_mod = _fresh("app")
    client = TestClient(app_mod.app)
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:hunter2").decode()}

    app_mod.ENGINES = []
    saved = client.get("/preferences?save=1&suggest=1", headers=headers,
                       follow_redirects=False)
    assert saved.status_code == 303
    assert saved.cookies["tv_suggest"] == "0"

    app_mod.ENGINES = [app_mod.BY_NAME["serper"]]
    saved = client.get("/preferences?save=1&suggest=1", headers=headers,
                       follow_redirects=False)
    assert saved.cookies["tv_suggest"] == "1"


# ------------------------------------------------------------------ quick answer
def test_answer_trigger_and_exa_cache(env):
    import asyncio
    import httpx
    answers = _fresh("answers")
    assert answers.triggered("What is Paris?")
    assert answers.triggered("What is Paris?  ")
    assert not answers.triggered("What is Paris")
    assert not answers.triggered("?")
    assert answers.question("What is Paris??") == "What is Paris"
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert request.headers["x-api-key"] == "exa-test"
        return httpx.Response(200, json={
            "answer": "Paris [1][2].",
            "citations": [
                {"title": "Britannica", "url": "https://britannica.com/place/Paris"},
                {"title": "Unsafe", "url": "javascript:alert(1)"},
                {"title": "Wikipedia", "url": "https://wikipedia.org/wiki/Paris"},
            ],
        })

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await answers.get(client, "exa", "What is Paris?", {"EXA_API_KEY": "exa-test"})
            second = await answers.get(client, "exa", " what is paris? ", {"EXA_API_KEY": "exa-test"})
            return first, second

    first, second = asyncio.run(exercise())
    assert first.ok and first.text == "Paris [1][2]."
    assert len(first.citations) == 3
    assert sum(bool(c.url) for c in first.citations) == 2
    assert first.citations[1].url == ""          # position [2] was not shifted
    assert second.cached_age is not None and second.ms == 0
    assert calls == 1


def test_answer_failures_are_not_cached(env):
    import asyncio
    import httpx
    answers = _fresh("answers")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"answer": "Paris", "citations": []})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            failed = await answers.get(client, "exa", "Capital?", {"EXA_API_KEY": "k"})
            recovered = await answers.get(client, "exa", "Capital?", {"EXA_API_KEY": "k"})
            return failed, recovered

    failed, recovered = asyncio.run(exercise())
    assert failed.error == "HTTP 503"
    assert recovered.ok and calls == 2


def test_valyu_answer_stream_collects_text_and_sources(env):
    import asyncio
    import httpx
    answers = _fresh("answers")
    payload = (
        'data: {"search_results":[{"title":"Britannica","url":"https://britannica.com/place/Paris"}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Paris is the capital "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"of France ([Britannica](https://britannica.com/place/Paris))."}}]}\n\n'
        'data: {"success":true,"cost":{"total_deduction_dollars":0.01}}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await answers.get(client, "valyu", "Capital of France?", {"VALYU_API_KEY": "k"})

    result = asyncio.run(exercise())
    assert result.ok
    assert result.text.startswith("Paris is the capital of France")
    assert result.citations[0].title == "Britannica"


def test_quick_answer_render_escapes_html_and_links_citations(env):
    answers = _fresh("answers")
    render = _fresh("render")
    result = answers.AnswerResult("exa", ms=1036, citations=[
        answers.Citation("Britannica", "https://britannica.com/place/Paris")],
        text="**Paris** [1][1] <script>alert(1)</script>\n[Britannica](https://britannica.com/place/Paris)")
    html = render._quick_answer(result, new_tab=True)
    assert "Quick answer" in html and "Exa" in html and "1.0s" in html
    assert '<span class="qcluster">' in html
    assert "<strong>Paris</strong>" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert 'aria-label="Source 1: Britannica"' in html
    assert 'target="_blank"' in html
    assert "1 source" in html


def test_answer_preference_lists_only_supported_configured_engines(env):
    providers = _fresh("providers")
    render = _fresh("render")
    choices = [providers.BY_NAME["exa"], providers.BY_NAME["tavily"]]
    html = render.preferences([], set(), "", "moderate", 10,
                              answer_engines=choices, answer=True, answer_engine="tavily")
    assert 'name="answer" value="1" checked' in html
    assert '<option value="exa">Exa</option>' in html
    assert '<option value="tavily" selected>Tavily</option>' in html
    assert '<option value="linkup"' not in html
    assert "end a query with ?" in html


def test_answer_preference_save_requires_a_supported_key(env):
    import base64
    from fastapi.testclient import TestClient
    app_mod = _fresh("app")
    client = TestClient(app_mod.app)
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:hunter2").decode()}
    app_mod.ENGINES = []
    saved = client.get("/preferences?save=1&answer=1&answer_engine=exa", headers=headers,
                       follow_redirects=False)
    assert saved.cookies["tv_answer"] == "0"
    assert saved.cookies["tv_answer_engine"].strip('"') == ""

    app_mod.ENGINES = [app_mod.BY_NAME["exa"], app_mod.BY_NAME["tavily"]]
    saved = client.get("/preferences?save=1&answer=1&answer_engine=tavily", headers=headers,
                       follow_redirects=False)
    assert saved.cookies["tv_answer"] == "1"
    assert saved.cookies["tv_answer_engine"] == "tavily"


def test_api_answer_runs_only_for_enabled_web_page_one_question(env, monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    app_mod = _fresh("app")
    app_mod.ENGINES = [app_mod.BY_NAME["exa"]]
    app_mod.ENV = {"EXA_API_KEY": "exa-test"}
    client = TestClient(app_mod.app)
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:hunter2").decode()}
    client.cookies.set("tv_answer", "1")
    client.cookies.set("tv_answer_engine", "exa")
    calls = []

    async def fake_search(query, *args, **kwargs):
        return {"results": [], "merged": [], "ranked": [], "window": [],
                "elapsed": 0.01, "cached_age": None}

    async def fake_answer(client, engine, query, env, use_cache=True):
        calls.append((engine, query, use_cache))
        return app_mod.answers.AnswerResult("exa", "Paris [1]", [
            app_mod.answers.Citation("Britannica", "https://britannica.com/place/Paris")], ms=1000)

    monkeypatch.setattr(app_mod, "search", fake_search)
    monkeypatch.setattr(app_mod.answers, "get", fake_answer)
    good = client.get("/api/search?q=Capital%20of%20France%3F", headers=headers)
    assert good.status_code == 200
    assert good.json()["quick_answer"]["answer"] == "Paris [1]"
    assert good.json()["quick_answer"]["citations"][0]["title"] == "Britannica"
    assert calls == [("exa", "Capital of France?", True)]

    for path in ("/api/search?q=Capital%20of%20France",
                 "/api/search?q=Capital%3F&page=2",
                 "/api/search?q=Capital%3F&tab=news"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["quick_answer"] is None
    assert len(calls) == 1

    client.cookies.set("tv_answer", "0")
    disabled = client.get("/api/search?q=Capital%3F", headers=headers)
    assert disabled.json()["quick_answer"] is None
    assert len(calls) == 1

    # Removing the chosen provider must not silently spend money with another one.
    client.cookies.set("tv_answer", "1")
    client.cookies.set("tv_answer_engine", "exa")
    app_mod.ENGINES = [app_mod.BY_NAME["tavily"]]
    removed = client.get("/api/search?q=Capital%3F", headers=headers)
    assert removed.json()["quick_answer"] is None
    assert len(calls) == 1
