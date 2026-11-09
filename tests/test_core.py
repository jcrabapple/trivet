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


# ------------------------------------------------------------------- rate limit
def test_rate_limit_trips_and_recovers(env):
    app = _fresh("app")
    app._RECENT.clear()
    for _ in range(app.RATE_LIMIT):
        assert app._rate_ok("bucket")
    assert not app._rate_ok("bucket")
    assert app._rate_ok("other-bucket")
