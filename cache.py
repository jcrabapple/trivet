"""SQLite query cache.

Every search bills each engine, so the same query asked twice (page refresh, back
button, a retyped query) should cost nothing the second time. The row stores the
*merged* result list, which is what the page renders, plus the per-engine status
that the UI reports.

Only successful searches are stored: caching a run where an engine 401'd would
pin that failure in place for the whole TTL and hide a recovered key.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any

CACHE_PATH = os.path.expanduser(os.environ.get("TRIVET_DB", "~/.cache/trivet/cache.db"))
CACHE_DIR = os.path.dirname(CACHE_PATH)
TTL_SECONDS = int(os.environ.get("TRIVET_CACHE_TTL", "900"))        # 15 min
NEWS_TTL_SECONDS = int(os.environ.get("TRIVET_NEWS_CACHE_TTL", "300"))  # 5 min
MAX_ROWS = 1000



def _harden(conn: sqlite3.Connection, path: str) -> None:
    """The database holds query history and key ciphertext; keep it owner-only."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except OSError:
            pass

def _connect() -> sqlite3.Connection:
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH, timeout=5)
    _harden(conn, CACHE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            key        TEXT PRIMARY KEY,
            query      TEXT NOT NULL,
            payload    TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON queries(created_at)")
    return conn


def normalize_query(query: str) -> str:
    """Fold case and whitespace so 'Fedora  44' and 'fedora 44' share an entry."""
    return " ".join(query.lower().split()).strip(" ?!.,;:\"'")


def make_key(query: str, engines: list[str], time_range: str, safesearch: str,
             depth: int, deep_policy: str = "auto", mode: str = "web") -> str:
    # deep_policy and mode change what actually ran, so both belong in the key:
    # otherwise switching tabs or policies replays the wrong result shape
    raw = json.dumps([normalize_query(query), sorted(engines), time_range, safesearch,
                      depth, deep_policy, mode])
    return hashlib.sha256(raw.encode()).hexdigest()


def get(key: str, ttl: int | None = None) -> dict[str, Any] | None:
    """Return the cached payload plus its age, or None on miss/expiry."""
    max_age = TTL_SECONDS if ttl is None else ttl
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM queries WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    payload, created = row[0], row[1]
    age = time.time() - created
    if age > max_age:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    data["age_s"] = age
    return data


def put(key: str, query: str, payload: dict[str, Any]) -> None:
    now = time.time()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO queries (key, query, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, query, json.dumps(payload), now))
            conn.execute("DELETE FROM queries WHERE created_at < ?", (now - 7 * 86400,))
            conn.execute(
                "DELETE FROM queries WHERE key NOT IN "
                "(SELECT key FROM queries ORDER BY created_at DESC LIMIT ?)", (MAX_ROWS,))
    except sqlite3.Error:
        pass  # a dead cache must never fail the search


def stats() -> dict[str, Any]:
    try:
        with _connect() as conn:
            rows, newest = conn.execute(
                "SELECT COUNT(*), MAX(created_at) FROM queries").fetchone()
    except sqlite3.Error:
        return {"entries": 0}
    return {"entries": rows, "ttl_s": TTL_SECONDS,
            "newest_age_s": round(time.time() - newest) if newest else None}
