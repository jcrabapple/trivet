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
