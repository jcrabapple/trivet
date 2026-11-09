"""Feedback loop: log what was shown and what was clicked, then learn from it.

The design follows what Metarank gets right about ranking systems, minus its stack:

  impression event  -> which URLs appeared at which positions (NOT just clicks;
                       a click with no impression cannot be evaluated or debiased)
  interaction event -> which of those URLs was opened
  feature snapshot  -> the signals that produced the position, stored per row, so a
                       different weight set can be replayed offline without refetching
  position bias     -> clicks concentrate at the top, so raw CTR overstates top results;
                       report an examination-weighted estimate alongside it
  cold start        -> a URL with zero impressions must not be scored 0; rates are
                       smoothed toward a prior, and aggregation is per-domain because
                       per-URL counts are hopelessly sparse at this scale

Everything lands in the same SQLite file as the query cache. No Flink, no Kafka, no
Redis: one user, one process.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from typing import Any, Iterable

DB_PATH = os.path.expanduser("~/.cache/trivet/cache.db")

# Prior CTR and pseudo-count for smoothing: a domain with 1 impression should not
# swing between 0% and 100%. 0.35/~8 matches the click-through rate of a decent
# first-page result, so new domains start neutral-ish rather than punished.
PRIOR_CTR = 0.35
PRIOR_WEIGHT = 8.0



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
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS impressions (
            impression_id TEXT NOT NULL,
            ts REAL NOT NULL,
            query TEXT NOT NULL,
            ranker TEXT NOT NULL,
            engines TEXT NOT NULL,
            position INTEGER NOT NULL,
            url TEXT NOT NULL,
            domain TEXT NOT NULL,
            score REAL NOT NULL,
            features TEXT NOT NULL,
            is_test INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (impression_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_imp_ts ON impressions(ts);
        CREATE INDEX IF NOT EXISTS idx_imp_query ON impressions(query);
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            impression_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            url TEXT NOT NULL,
            is_test INTEGER NOT NULL DEFAULT 0,
            UNIQUE (impression_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_click_imp ON clicks(impression_id);
    """)
    return conn


def new_impression_id() -> str:
    return secrets.token_urlsafe(9)


RETENTION_DAYS = 90


def _prune(conn) -> None:
    """Impressions and clicks grow by `per` rows per search and were never removed."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    for table in ("impressions", "clicks"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        except sqlite3.Error:
            pass


def log_impression(impression_id: str, query: str, ranker: str, engines: Iterable[str],
                   hits: list[Any], start_position: int = 1, is_test: bool = False) -> None:
    """Store the page that was shown, one row per position.

    `hits` are ranking.Ranked objects so each row keeps its feature vector: that is
    what makes offline replay possible later without paying the engines again.
    """
    rows = []
    now = time.time()
    for offset, item in enumerate(hits):
        hit = item.hit
        rows.append((impression_id, now, query, ranker, ",".join(sorted(engines)),
                     start_position + offset, hit.url, hit.domain,
                     float(item.score), json.dumps(item.features.as_dict()),
                     1 if is_test else 0))
    if not rows:
        return
    try:
        with _conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO impressions (impression_id, ts, query, ranker, engines,"
                " position, url, domain, score, features, is_test)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            _prune(conn)
    except sqlite3.Error:
        pass   # telemetry must never break a search


def log_click(impression_id: str, position: int, url: str, is_test: bool = False) -> bool:
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO clicks (ts, impression_id, position, url, is_test)"
                " VALUES (?,?,?,?,?)", (time.time(), impression_id, position, url,
                                        1 if is_test else 0))
    except sqlite3.Error:
        return False
    return True


# ------------------------------------------------------------------- aggregation
def examination_probability(position: int) -> float:
    """How likely a result at this position was looked at.

    Standard position-based examination model, normalized so position 1 = 1.0.
    Used to weight clicks when estimating CTR, because a click at position 1 is
    mostly evidence of position, not of quality.
    """
    return 1.0 / (1.0 + 0.85 * max(0, position - 1))


def position_report(include_test: bool = False) -> list[dict[str, Any]]:
    flt = "" if include_test else "WHERE i.is_test = 0"
    q = f"""
        SELECT i.position,
               COUNT(*) AS impressions,
               SUM(CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END) AS clicks
        FROM impressions i
        LEFT JOIN clicks c
          ON c.impression_id = i.impression_id AND c.position = i.position
        {flt}
        GROUP BY i.position ORDER BY i.position
    """
    with _conn() as conn:
        return [{"position": p, "impressions": n, "clicks": c,
                 "ctr": (c / n) if n else 0.0,
                 "exam": round(examination_probability(p), 3)}
                for p, n, c in conn.execute(q)]


def domain_report(include_test: bool = False) -> list[dict[str, Any]]:
    """Smoothed CTR per domain, plus the item-level counts behind it.

    Per-URL CTR is unusable here (one user, a few hundred URLs); per-domain is the
    lowest granularity that accumulates signal, which is exactly Metarank's
    field-scoped-rate point applied to search instead of products.
    """
    flt = "" if include_test else "WHERE i.is_test = 0"
    q = f"""
        SELECT i.domain,
               COUNT(*) AS impressions,
               SUM(CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END) AS clicks
        FROM impressions i
        LEFT JOIN clicks c
          ON c.impression_id = i.impression_id AND c.position = i.position
        {flt}
        GROUP BY i.domain
        ORDER BY impressions DESC
    """
    out = []
    with _conn() as conn:
        for domain, impressions, clicks in conn.execute(q):
            smoothed = ((clicks or 0) + PRIOR_CTR * PRIOR_WEIGHT) / (impressions + PRIOR_WEIGHT)
            out.append({"domain": domain, "impressions": impressions,
                        "clicks": clicks or 0,
                        "ctr": round((clicks or 0) / impressions, 3) if impressions else 0.0,
                        "smoothed_ctr": round(smoothed, 3)})
    return out


def totals(include_test: bool = False) -> dict[str, int]:
    flt = "" if include_test else "WHERE is_test = 0"   # single-table queries
    with _conn() as conn:
        impressions = conn.execute(
            f"SELECT COUNT(*) FROM impressions {flt}").fetchone()[0]
        clicks = conn.execute(f"SELECT COUNT(*) FROM clicks {flt}").fetchone()[0]
        sessions = conn.execute(
            f"SELECT COUNT(DISTINCT impression_id) FROM impressions {flt}").fetchone()[0]
    return {"impressions": impressions, "clicks": clicks, "pages": sessions}


def load_replay_set(include_test: bool = False) -> list[dict[str, Any]]:
    """Impressions grouped per page, with the clicked positions for that page."""
    flt = "" if include_test else "WHERE i.is_test = 0"
    pages: dict[str, dict[str, Any]] = {}
    with _conn() as conn:
        for (iid, query, ranker, pos, url, domain, features) in conn.execute(
                f"SELECT impression_id, query, ranker, position, url, domain, features "
                f"FROM impressions i {flt} ORDER BY impression_id, position"):
            page = pages.setdefault(iid, {"id": iid, "query": query, "ranker": ranker,
                                          "items": [], "clicked": set()})
            page["items"].append({"position": pos, "url": url, "domain": domain,
                                  "features": json.loads(features)})
        for (iid, pos) in conn.execute(
                f"SELECT impression_id, position FROM clicks c "
                f"{'WHERE is_test = 0' if not include_test else ''}"):
            if iid in pages:
                pages[iid]["clicked"].add(pos)
    return [p for p in pages.values() if p["clicked"]]
