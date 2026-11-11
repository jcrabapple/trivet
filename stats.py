"""Per-engine timing history, so "which engine is slow" is a measurement.

The results page already shows one query's timings; this keeps them so the app can
answer the aggregate question too (p50/p95, success rate) over the last day and week.
One row per engine per search: a few thousand rows a month at this volume.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Iterable

DB_PATH = os.path.expanduser(os.environ.get("TRIVET_DB", "~/.cache/trivet/cache.db"))
RETENTION_DAYS = 45



def _harden(conn: sqlite3.Connection, path: str) -> None:
    """The database holds query history and key ciphertext; keep it owner-only."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except OSError:
            pass

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    _harden(conn, DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engine_timings (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     REAL NOT NULL,
            engine TEXT NOT NULL,
            ms     INTEGER NOT NULL,
            ok     INTEGER NOT NULL,
            error  TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eng_ts ON engine_timings(ts)")
    return conn


def record(results: Iterable[Any]) -> None:
    """Store one row per engine. Never raises: telemetry must not break a search."""
    rows = []
    now = time.time()
    for r in results:
        if getattr(r, "skipped", False):
            continue          # not a measurement: the call never happened
        rows.append((now, r.name, int(r.ms), 1 if r.ok else 0, r.error))
    if not rows:
        return
    try:
        with _conn() as conn:
            conn.executemany(
                "INSERT INTO engine_timings (ts, engine, ms, ok, error) VALUES (?,?,?,?,?)",
                rows)
            conn.execute("DELETE FROM engine_timings WHERE ts < ?",
                         (now - RETENTION_DAYS * 86400,))
    except sqlite3.Error:
        pass


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[idx]


def summary(hours: int = 24) -> list[dict[str, Any]]:
    """Per-engine stats over the window, newest engines first by sample count."""
    since = time.time() - hours * 3600
    out: list[dict[str, Any]] = []
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT engine, ms, ok, error FROM engine_timings WHERE ts >= ?",
                (since,)).fetchall()
    except sqlite3.Error:
        return out
    by_engine: dict[str, dict[str, Any]] = {}
    for engine, ms, ok, error in rows:
        agg = by_engine.setdefault(engine, {"engine": engine, "samples": 0, "fails": 0,
                                            "times": [], "errors": {}})
        agg["samples"] += 1
        if ok:
            agg["times"].append(int(ms))
        else:
            agg["fails"] += 1
            if error:
                agg["errors"][error] = agg["errors"].get(error, 0) + 1
    for agg in by_engine.values():
        times = agg.pop("times")
        agg["p50"] = _percentile(times, 50)
        agg["p95"] = _percentile(times, 95)
        agg["mean"] = int(sum(times) / len(times)) if times else 0
        agg["max"] = max(times) if times else 0
        agg["success"] = round(100 * (agg["samples"] - agg["fails"]) / agg["samples"], 1) \
            if agg["samples"] else 0.0
        agg["last_error"] = max(agg["errors"], key=agg["errors"].get) if agg["errors"] else ""
        agg.pop("errors")
        out.append(agg)
    out.sort(key=lambda a: (-a["samples"], a["engine"]))
    return out


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Last N engine calls, for a per-query view of variance."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT ts, engine, ms, ok FROM engine_timings ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
    except sqlite3.Error:
        return []
    return [{"ts": ts, "engine": e, "ms": ms, "ok": bool(ok)} for ts, e, ms, ok in rows]
