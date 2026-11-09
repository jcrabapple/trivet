"""Merge layer: canonicalize URLs, then fuse ranked lists with Reciprocal Rank Fusion.

RRF is the boring reliable choice because the engines score on incomparable
scales: Exa returns a similarity float, Valyu returns relevance_score, Keenable
returns none, Brave returns its own ranking. Fusing on *rank* sidesteps the
question and rewards agreement across engines for free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from providers import EngineResult, Hit

TRACKING_PREFIXES = ("utm_", "mc_", "hs_", "pk_", "oly_", "vero_")
TRACKING_EXACT = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "twclid",
    "ref", "ref_src", "referrer", "source", "src", "s", "si", "spm",
    "share", "share_id", "cmpid", "campaign", "__twitter_impression",
}
DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonical_url(url: str) -> str:
    """Stable dedupe key: drop scheme drift, www, tracking params, fragments."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port and DEFAULT_PORTS.get(parts.scheme) != str(port):
        host = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]

    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl in TRACKING_EXACT or kl.startswith(TRACKING_PREFIXES):
            continue
        kept.append((k, v))
    return urlunsplit(("https", host, path, urlencode(sorted(kept)), ""))


def _title_tokens(title: str) -> set[str]:
    """Token set for fuzzy duplicate detection across syndicated copies.

    Site-name suffixes ("... | Example News") and truncation ellipses differ
    between mirrors, so exact title hashing misses them; token-set comparison
    catches the copy without merging unrelated articles.
    """
    base = title.split(" | ")[0].split(" — ")[0].split(" - ")[0]
    norm = "".join(ch.lower() if (ch.isalnum() or ch == " ") else " " for ch in base)
    return {t for t in norm.split() if len(t) > 2 and t != "the"}


def _similar(a: set[str], b: set[str], threshold: float = 0.7) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold


@dataclass
class MergedHit:
    title: str
    url: str
    snippet: str = ""
    engines: list[str] = field(default_factory=list)
    score: float = 0.0        # RRF fusion score
    best_rank: int = 999
    published: str | None = None
    duplicates: list[str] = field(default_factory=list)
    # per-engine provenance: [{"e": engine, "r": rank, "s": native score or None}]
    # ranking.py needs the ranks to separate "one engine loved it" from "everyone liked it"
    sources: list[dict] = field(default_factory=list)

    @property
    def agreement(self) -> int:
        return len(self.engines)

    @property
    def domain(self) -> str:
        host = urlsplit(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host


def merge(results: list[EngineResult], k: int = 60,
          weights: dict[str, float] | None = None,
          collapse_syndicated: bool = False) -> list[MergedHit]:
    """Fuse every successful engine's ranked list into one ordering.

    A hit seen by three engines outranks a hit seen by one, even a rank-1 single
    sighting. Consensus across independent indexes is a stronger relevance signal
    than any single vendor's score, and it costs nothing extra.
    """
    weights = weights or {}
    by_key: dict[str, MergedHit] = {}
    clusters: list[tuple[set[str], str]] = []   # (title tokens, canonical key)

    for res in results:
        if not res.ok:
            continue
        w = weights.get(res.name, 1.0)
        for hit in res.hits:
            key = canonical_url(hit.url)
            if collapse_syndicated and key not in by_key:
                tokens = _title_tokens(hit.title)
                for cluster_tokens, cluster_key in clusters:
                    if _similar(tokens, cluster_tokens):
                        key = cluster_key
                        break
                else:
                    clusters.append((tokens, key))
            entry = by_key.get(key)
            if entry is None:
                entry = MergedHit(title=hit.title, url=hit.url, snippet=hit.snippet,
                                  published=hit.published)
                by_key[key] = entry
            elif entry.url != hit.url and hit.url not in entry.duplicates:
                entry.duplicates.append(hit.url)
            if res.name not in entry.engines:
                entry.engines.append(res.name)
            entry.sources.append({"e": res.name, "r": hit.rank, "s": hit.score})
            entry.score += w / (k + hit.rank)
            entry.best_rank = min(entry.best_rank, hit.rank)
            if not entry.snippet and hit.snippet:
                entry.snippet = hit.snippet
            if not entry.published and hit.published:
                entry.published = hit.published
            if len(entry.title) < 12 and len(hit.title) > len(entry.title):
                entry.title = hit.title

    return sorted(by_key.values(), key=lambda h: (-h.score, h.best_rank))


def engine_consensus(results: list[EngineResult]) -> dict[str, float]:
    """Share of an engine's results that another engine also returned.

    Low consensus means that engine contributes genuine recall; high consensus
    means you are paying twice for the same index.
    """
    ok = [r for r in results if r.ok and r.hits]
    if len(ok) < 2:
        return {}
    keys = {r.name: {canonical_url(h.url) for h in r.hits} for r in ok}
    out = {}
    for name, mine in keys.items():
        others = set().union(*[v for n, v in keys.items() if n != name])
        out[name] = round(len(mine & others) / len(mine), 2) if mine else 0.0
    return out
