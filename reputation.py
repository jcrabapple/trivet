"""Domain reputation: mark results that live on ad, tracker, malware or spam hosts.

Three sources, cheapest and most specific first:

1. A local override file (`~/.config/trivet/domains.json`) for domains you want
   flagged or allowed, including content farms the DNS lists do not cover.
2. AdGuard Home's filtering engine over its own lists. This is the good one: it
   already holds AdGuard DNS filter, Hagezi Multi Pro (ads/tracking/telemetry) and
   Hagezi Threat Intelligence (malware/phishing), ~1.7M rules, maintained upstream.
   A host is "malware" if the threat-intel list matched, otherwise "ads".
3. Structural heuristics for SEO-shaped hostnames, which no DNS list can know.

A blocklist hit is evidence about the *host*, not proof the page is worthless: a
vendor blog on a flagged CDN should not be buried. Penalties are moderate and the
UI shows the reason, so a wrong call is visible instead of silent.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

# Optional: point these at any AdGuard Home instance to use its filter lists as a
# reputation source. Unset means reputation falls back to the local lists and the
# structural heuristics only.
AGH_URL = os.environ.get("TRIVET_ADGUARD_URL", "").rstrip("/")
AGH_PASSWORD_FILE = os.path.expanduser(
    os.environ.get("TRIVET_ADGUARD_PASSWORD_FILE", "~/.config/trivet/adguard-password"))
OVERRIDE_FILE = os.path.expanduser("~/.config/trivet/domains.json")
DB_PATH = os.path.expanduser(os.environ.get("TRIVET_DB", "~/.cache/trivet/cache.db"))
CACHE_TTL = 7 * 86400          # reputation changes slowly; a week is plenty
CHECK_TIMEOUT = 1.2

# categories, ordered by severity; anything not listed is "none"
CATEGORIES = ("malware", "spam", "suspicious", "ads", "none", "unknown")

SPAM_HOST_RE = re.compile(
    r"(best|top|review|guide|cheap|deal)s?\d{4}"
    r"|\d{4}(best|top|review|guide|deals?)"
    r"|(^|\.)(coupon|coupons|deals?|discount)s?[a-z0-9-]*\.(com|net|info)$", re.I)


@dataclass
class Reputation:
    domain: str
    category: str = "unknown"     # malware | spam | suspicious | ads | none | unknown
    detail: str = ""              # filter or rule that matched
    source: str = ""              # adguard | list | heuristic

    @property
    def flagged(self) -> bool:
        return self.category in ("malware", "spam", "suspicious", "ads")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS domain_rep (
            domain     TEXT PRIMARY KEY,
            category   TEXT NOT NULL,
            detail     TEXT NOT NULL,
            source     TEXT NOT NULL,
            checked_at REAL NOT NULL
        );
    """)
    return conn


SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/domains.json")


def _read_list(path: str) -> dict[str, list[str]]:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: [str(d).lower() for d in data.get(k, [])]
            for k in ("allow", "spam", "suspicious")}


def load_overrides() -> dict[str, Any]:
    """Seed list first, then the user's file. Lists are unioned; `allow` wins later."""
    merged: dict[str, set[str]] = {"allow": set(), "spam": set(), "suspicious": set()}
    for path in (SEED_FILE, OVERRIDE_FILE):
        for key, values in _read_list(path).items():
            merged[key].update(values)
    return {k: sorted(v) for k, v in merged.items()}


def _matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _agh_password() -> str:
    try:
        return open(AGH_PASSWORD_FILE).read().strip()
    except OSError:
        return ""


def _filter_names(status: dict) -> dict[int, str]:
    return {f.get("id"): f.get("name", "") for f in (status.get("filters") or [])}


class ReputationService:
    def __init__(self) -> None:
        self._mem: dict[str, Reputation] = {}
        self._filter_names: dict[int, str] = {}

    # ------------------------------------------------------------- cache layer
    def _cached(self, domain: str) -> Reputation | None:
        if domain in self._mem:
            return self._mem[domain]
        try:
            with _conn() as conn:
                row = conn.execute(
                    "SELECT category, detail, source, checked_at FROM domain_rep"
                    " WHERE domain = ?", (domain,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or time.time() - row[3] > CACHE_TTL:
            return None
        rep = Reputation(domain, row[0], row[1], row[2])
        self._mem[domain] = rep
        return rep

    def _store(self, reps: list[Reputation]) -> None:
        if not reps:
            return
        now = time.time()
        try:
            with _conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO domain_rep (domain, category, detail, source,"
                    " checked_at) VALUES (?,?,?,?,?)",
                    [(r.domain, r.category, r.detail, r.source, now) for r in reps])
        except sqlite3.Error:
            pass
        for r in reps:
            self._mem[r.domain] = r

    # ---------------------------------------------------------------- checking
    def _from_overrides(self, host: str, ov: dict[str, Any]) -> Reputation | None:
        if _matches(host, ov["allow"]):
            return Reputation(host, "none", "local allowlist", "list")
        if _matches(host, ov["spam"]):
            return Reputation(host, "spam", "local spam list", "list")
        if _matches(host, ov["suspicious"]):
            return Reputation(host, "suspicious", "local suspicious list", "list")
        return None

    def _heuristic(self, host: str, url: str = "") -> Reputation | None:
        if SPAM_HOST_RE.search(host):
            return Reputation(host, "spam", "SEO-shaped hostname", "heuristic")
        # Warez and link-selling hosts: no DNS list covers these because they are not
        # ad servers. Match only unambiguous tokens on the host (there is no legitimate
        # "keygen.com"), plus a milder path-scoped rule, because a bare "crack" would
        # flag cracked.com.
        if re.search(r"(keygen|warez|nulled|serialz)", host):
            return Reputation(host, "suspicious", "warez-shaped hostname", "heuristic")
        path = urlsplit(url).path.lower() if url else ""
        if re.search(r"(crack|keygen|activator)", host + path) and re.search(
                r"(download|free|crack|keygen|activator)", path):
            return Reputation(host, "suspicious", "warez page pattern", "heuristic")
        if re.search(r"(backlink|linkfarm|link-building)", host):
            return Reputation(host, "suspicious", "link-selling hostname", "heuristic")
        return None

    async def _agh_lookup(self, client: httpx.AsyncClient, password: str,
                          host: str) -> Reputation | None:
        """Ask AdGuard Home which filter (if any) blocks this host."""
        if not AGH_URL:
            return None          # no instance configured: local lists and heuristics only
        try:
            r = await client.get(f"{AGH_URL}/control/filtering/check_host",
                                 params={"name": host},
                                 auth=("admin", password),
                                 timeout=CHECK_TIMEOUT)
            data = r.json()
        except Exception:  # noqa: BLE001 - reputation must never break a search
            return None
        if not str(data.get("reason", "")).lower().startswith("filtered"):
            return Reputation(host, "none", "", "adguard")
        fid = data.get("filter_id") or 0
        name = self._filter_names.get(fid, "")
        if "threat" in name.lower() or "malware" in name.lower() or "phishing" in name.lower():
            return Reputation(host, "malware", name or data.get("rule", ""), "adguard")
        return Reputation(host, "ads", name or data.get("rule", ""), "adguard")

    async def check_many(self, urls: Iterable[str]) -> dict[str, Reputation]:
        """Resolve reputation for a page's results, keyed by host, cache-first.

        Takes URLs rather than hosts so path-sensitive heuristics (warez download
        pages) can see the path; the cache and the resolver are host-scoped.
        """
        sample: dict[str, str] = {}
        for u in urls:
            if not u:
                continue
            host = urlsplit(u).netloc.lower()
            host = host[4:] if host.startswith("www.") else host
            sample.setdefault(host, u)
        unique = list(sample)
        out: dict[str, Reputation] = {}
        pending: list[str] = []

        overrides = load_overrides()
        for host in unique:
            # local lists are authoritative and free, so they precede the cache:
            # a domain added to the list must take effect even if AGH said "none"
            # last week and that answer is still cached.
            decided = self._from_overrides(host, overrides)
            if decided:
                out[host] = decided
                self._store([decided])
                continue
            cached = self._cached(host)
            if cached:
                out[host] = cached
                continue
            heuristic = self._heuristic(host, sample.get(host, ""))
            if heuristic:
                out[host] = heuristic
                self._store([heuristic])
                continue
            pending.append(host)

        if not pending:
            return out

        password = _agh_password()
        if not password:
            for host in pending:
                out[host] = Reputation(host, "unknown", "no AdGuard credentials", "")
            return out

        async with httpx.AsyncClient() as client:
            try:
                status = (await client.get(f"{AGH_URL}/control/filtering/status",
                                           auth=("admin", password),
                                           timeout=CHECK_TIMEOUT)).json()
                self._filter_names = _filter_names(status)
            except Exception:  # noqa: BLE001
                self._filter_names = {}

            results = await _gather_limited(
                [self._agh_lookup(client, password, h) for h in pending], limit=8)

        fresh: list[Reputation] = []
        for host, rep in zip(pending, results):
            rep = rep or Reputation(host, "unknown", "AdGuard unreachable", "")
            out[host] = rep
            if rep.category != "unknown":
                fresh.append(rep)
        self._store(fresh)
        return out


async def _gather_limited(coros: list, limit: int) -> list:
    """Run lookups with a small concurrency cap: the resolver is a local service."""
    import asyncio
    sem = asyncio.Semaphore(limit)

    async def one(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(one(c) for c in coros))


SERVICE = ReputationService()


async def check_domains(urls: Iterable[str]) -> dict[str, Reputation]:
    return await SERVICE.check_many(urls)
