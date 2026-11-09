"""Ranking: turn fused engine results into an ordered list with explainable signals.

Stage 1 (merge.py) fuses engine ranks with RRF, which captures consensus and
nothing else: it never looks at the query, the title, the source, or the date.
That is why a 2022 article can outrank this month's release notes on a "best X
2026" query.

This stage scores each candidate on independent features and combines them with
weights that can be tuned from a JSON file without touching code:

    score = w_rrf·consensus + w_rank·engine_rank + w_agree·sources
          + w_title·title_match + w_snippet·snippet_match
          + w_domain·domain_prior + w_fresh·freshness + w_quality·snippet_quality
          - penalties

Every weight is visible in /debug/rank, so a bad ordering can be attributed to a
signal instead of argued about. Tuning without that breakdown is guesswork.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import quality
import smallweb

CONFIG_PATH = os.path.expanduser("~/.config/trivet/ranking.json")

DEFAULT_CONFIG: dict[str, Any] = {
    # Tuned by tune_rank.py against eval/queries.json (18 queries) and checked on
    # eval/holdout.json (4 unseen queries). Hand-picked values were worse than plain
    # RRF; these beat it on both sets. Re-tune after adding queries, not before.
    "weights": {
        "rrf": 0.25,
        "rank": 0.10,
        "agree": 0.04,
        "title": 0.10,
        "snippet": 0.05,
        "domain": 0.22,
        "fresh": 0.05,
        # tech content decays much faster than prose: a 2023 "best X" list is a
        # different answer from a 2026 one, even when both are still online
        # 0.0 on purpose: a positive recency weight promoted whoever re-dates a
        # listicle, and measured worse on both the doc queries and the review
        # queries. Staleness is handled by the penalties below instead.
        "fresh_tech": 0.0,
        "quality": 0.05,
    },
    "penalties": {
        "empty_title": 0.20,
        "short_title": 0.08,       # fewer than 15 characters
        "stale_for_year": 0.12,    # query names a year, page is older
        "tech_stale_18m": 0.20,    # tech choice: 18 months old is a different answer
        "tech_stale_3y": 0.32,     # three years old is archaeology
        "title_year_last": 0.15,   # title advertises last year, query wants current
        "title_year_old": 0.22,    # title advertises an older year
        "encyclopedia_on_choice": 0.90,  # "which browser", not "what is a browser"
        "seo_listicle": 0.40,            # roundup markers x (1 - publisher trust)
        "thin_snippet": 0.04,
        # domain reputation, from AdGuard Home lists and local overrides
        "malware": 0.40,           # threat-intel list: do not send anyone there
        "spam": 0.22,              # content farms, SEO shells
        "ads": 0.10,               # ad/tracker host; still may host a real page
        "suspicious": 0.08,
    },
    "fresh_half_life_days": 240,
    "diversify_max_per_domain": 2,
    "engine_weights": {},          # per-engine trust multiplier on the RRF term
}

STOPWORDS = {
    "a", "an", "the", "of", "for", "to", "in", "on", "and", "or", "is", "are",
    "was", "were", "be", "with", "how", "what", "why", "when", "vs", "v",
    "best", "top", "good", "new", "my", "your", "it", "this", "that",
    # comparison/intent words: they describe the answer, not the topic, and every
    # result mentions them, so counting them dilutes real topical coverage
    "which", "better", "versus", "should", "use", "using", "does", "do", "can",
}

# Software/hardware/computing nouns. These do not make a query "recent" by
# themselves, but they do mean the answer has a shelf life.
TECH_RE = re.compile(
    r"\b(android|ios|ipados|iphone|ipad|macos|windows|linux|chromeos|fedora|"
    r"ubuntu|debian|gentoo|opensuse|arch linux|wayland|x11|hyprland|"
    r"browser|browsers|app|apps|software|sdk|api|framework|library|plugin|"
    r"extension|driver|firmware|gpu|cpu|ram|ssd|nvme|laptop|router|nas|"
    r"docker|dockerfile|podman|kubernetes|k8s|python|rust|golang|javascript|"
    r"typescript|react|vue|svelte|node|npm|deno|sql|postgres|postgresql|"
    r"mysql|mariadb|sqlite|redis|nginx|apache|caddy|cli|terminal|bash|zsh|"
    r"shell|git|ssh|systemd|cron|vpn|dns|codec|programming|developer|coding|"
    r"compile|kernel|distro|flatpak|snap|esp32|esp8266|arduino|raspberry pi|"
    r"app store|play store)\b",
    re.I)

# Established tech publishers. Freshness alone promotes whoever re-dates a listicle
# every quarter, so recency is paired with a source prior or the winners are content
# farms with 2026 in the title.
REPUTABLE_TECH = (
    "zdnet.com", "androidauthority.com", "howtogeek.com", "tomsguide.com",
    "tomshardware.com", "pcmag.com", "arstechnica.com", "theverge.com", "wired.com",
    "engadget.com", "cnet.com", "techradar.com", "xda-developers.com",
    "androidpolice.com", "9to5google.com", "androidcentral.com", "ghacks.net",
    "bleepingcomputer.com", "phoronix.com", "lwn.net", "theregister.com",
    "rtings.com", "slashgear.com", "digitaltrends.com", "thewirecutter.com",
    "consumerreports.org", "popularmechanics.com",
)

# Official app/extension storefronts. For "sync bookmarks extension chrome" the
# listing IS the answer, and these domains were scoring zero while a GitHub issue
# thread scored +0.80.
STOREFRONT_RE = re.compile(
    r"^(chromewebstore\.google\.com/detail/"
    r"|chrome\.google\.com/webstore/detail/"
    r"|addons\.mozilla\.org/[a-z-]+/firefox/addon/"
    r"|microsoftedge\.microsoft\.com/addons/detail/"
    r"|apps\.apple\.com/[a-z]{2}/app/"
    r"|play\.google\.com/store/apps/details)", re.I)

# github.com is reference material when it is a repo or a file, and UGC when it is
# an issue thread, a pull request or a discussion.
GITHUB_UGC_RE = re.compile(r"/(issues|pull|pulls|discussions|search|sponsors)(/|$)", re.I)

# A *choice* query wants current options, not the concept. "fedora 44 release date"
# is equally tech but an encyclopedia entry is a fine answer to it, so the demotion
# is gated on the query asking for a pick rather than a fact.
CHOICE_RE = re.compile(
    r"\b(best|top|vs|versus|which|compare|comparison|alternative|alternatives|"
    r"recommend|recommended|should i|worth it)\b", re.I)

# General encyclopedias answer "what is X" well and "which X should I use in 2026"
# badly. Not penalised globally, only demoted when the query is about software.
GENERAL_REFERENCE = ("wikipedia.org", "britannica.com", "encyclopedia.com",
                     "dictionary.com", "merriam-webster.com", "wikidata.org")

RECENCY_RE = re.compile(
    r"\b(20[2-9]\d|latest|newest|new|news|today|tonight|current|"
    r"updated?|changelog|announcement|roadmap)\b", re.I)
# "release date", "history", "when did X ship" ask for a fixed fact: a page updated
# yesterday is not a better answer than the reference page, so freshness stays off.
FACT_RE = re.compile(r"\b(date|history|historically|when|timeline|since)\b", re.I)
YEAR_RE = re.compile(r"\b(20[2-9]\d)\b")

# Hosts that publish the thing being searched for, or that curate it well.
TRUSTED_SUFFIXES = (
    ".gov", ".edu", ".mil", ".int",
    "wikipedia.org", "wikimedia.org", "arxiv.org", "acm.org", "ieee.org",
    "docs.python.org", "python.org", "mozilla.org", "developer.mozilla.org",
    "kernel.org", "fedoraproject.org", "debian.org", "archlinux.org",
    "github.com", "gitlab.com", "stackoverflow.com", "serverfault.com",
    "rfc-editor.org", "ietf.org", "w3.org", "unicode.org",
    "rust-lang.org", "go.dev", "nodejs.org", "postgresql.org", "sqlite.org",
    "nginx.org", "openssl.org", "cloudflare.com", "kubernetes.io",
    "docs.docker.com", "podman.io", "systemd.io", "freedesktop.org",
)
# Vendor and project blogs that are the primary source for their own product.
VENDOR_HOSTS = (
    "redhat.com", "docs.fedoraproject.org", "fedoramagazine.org",
    "developers.cloudflare.com", "blog.cloudflare.com", "help.kagi.com",
    "doc.rust-lang.org", "docs.rs", "docs.docker.com", "docs.espressif.com",
    "learn.microsoft.com", "developer.apple.com", "developer.android.com",
    "support.apple.com", "wiki.archlinux.org", "man7.org", "ss64.com",
)
# Subdomains of a trusted domain that host user discussion, not reference text:
# a forum thread about systemd timers is not the systemd documentation.
UGC_PREFIXES = ("discussion.", "community.", "forums.", "forum.", "packages.",
                "ask.", "answers.", "users.", "bugs.", "issues.", "gitlab.",
                "lists.", "mail.", "old.", "beta.",
                # gist.github.com is a pastebin, not the project's documentation
                "gist.", "snippets.", "paste.")

# Aggregators and SEO-shaped domains: not banned, just discounted.
DISCOUNTED_SUFFIXES = (
    "pinterest.com", "quora.com", "answers.com", "ehow.com", "wikihow.com",
    ".info", ".click", ".top", ".buzz", ".loan", ".win",
)
SOCIAL_HOSTS = ("reddit.com", "news.ycombinator.com", "lobste.rs", "stackexchange.com")

NUMERIC_PATH_RE = re.compile(r"(?:/[^/]*\d{5,}[^/]*)", re.I)


def load_config() -> dict[str, Any]:
    """Default weights, overlaid by ~/.config/trivet/ranking.json if present.

    Merged rather than replaced so a tuning file can override one weight without
    restating the whole table. Reloads when the file's mtime changes.
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return cfg
    if _cache.get("mtime") == mtime:
        return _cache["cfg"]
    try:
        with open(CONFIG_PATH) as fh:
            user = json.load(fh)
    except (OSError, ValueError):
        return cfg
    for key, val in user.items():
        if isinstance(val, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(val)
        else:
            cfg[key] = val
    _cache.update({"mtime": mtime, "cfg": cfg})
    return cfg


_cache: dict[str, Any] = {}


@dataclass
class Features:
    rrf: float = 0.0
    rank: float = 0.0
    agree: float = 0.0
    title: float = 0.0
    snippet: float = 0.0
    domain: float = 0.0
    fresh: float = 0.0
    quality: float = 0.0
    penalties: float = 0.0
    published_days: int | None = None
    intent: str = "general"
    reputation: str = "none"
    seo: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


@dataclass
class Ranked:
    hit: Any                     # merge.MergedHit
    score: float
    features: Features = field(default_factory=Features)


# ------------------------------------------------------------------ tokenizing
def query_terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9+#.\-]*", query.lower())
            if t not in STOPWORDS and len(t) > 1]


def detect_intent(query: str) -> str:
    tech = bool(TECH_RE.search(query))
    if tech:
        # "release date" style fact lookups inside tech keep the fact behaviour
        if FACT_RE.search(query):
            return "tech"
        return "tech_recency" if RECENCY_RE.search(query) else "tech"
    if FACT_RE.search(query):
        return "general"
    return "recency" if RECENCY_RE.search(query) else "general"


def _years_in(query: str) -> list[int]:
    return [int(y) for y in YEAR_RE.findall(query)]


def _coverage(terms: list[str], text: str) -> float:
    if not terms:
        return 0.0
    low = text.lower()
    hits = sum(1 for t in terms if t in low)
    return hits / len(terms)


def _title_match(terms: list[str], title: str) -> float:
    """Coverage, rewarded for covering everything and discounted for stuffing.

    Fractional coverage alone ranks "best fedora 44 gnome 50 changes 2026 review
    guide" above "Fedora 44 ChangeSet": long titles hit more terms by accident.
    """
    if not terms:
        return 0.0
    coverage = _coverage(terms, title)
    tokens = len(re.findall(r"[a-z0-9]+", title.lower()))
    # density factor: 1.0 at a normal title length, decaying as the title balloons
    # gentle: a 25-word news headline is not keyword stuffing. Only long AND
    # low-coverage titles get discounted, and never below half weight.
    slack = max(0, tokens - 2 * len(terms))
    density = max(0.5, 1.0 / (1.0 + slack / 12.0))
    complete = 1.0 if coverage >= 0.999 else 0.0
    return min(1.0, (0.65 * coverage + 0.35 * complete) * density)


def _phrase_bonus(query: str, text: str) -> float:
    q = " ".join(query.lower().split())
    return 0.25 if len(q) > 6 and q in " ".join(text.lower().split()) else 0.0


# --------------------------------------------------------------- domain prior
def host_of(url: str) -> str:
    host = (urlsplit(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


# Category nouns that make a bare query a "which one" question: "Android Web
# browsers" is a list request even without "best". A plain concept query ("rust
# ownership borrowing") must not qualify, or freshness demotes the canonical page.
CATEGORY_NOUN_RE = re.compile(
    r"\b(browsers|apps|clients|tools|editors|players|managers|distros|suites|"
    r"ides|servers|extensions|plugins|themes|launchers|keyboards|mice|laptops)\b",
    re.I)


def is_choice_query(query: str, terms: list[str]) -> bool:
    """A pick-my-software query: explicit choice words, or a category-noun query
    like "Android Web browsers" where every result is a candidate list."""
    if CHOICE_RE.search(query):
        return True
    return bool(TECH_RE.search(query) and CATEGORY_NOUN_RE.search(query))


def _domain_prior(host: str, url: str, intent: str = "general",
                  choice: bool = False) -> tuple[float, float, float]:
    """Returns (prior in -1..1, penalty in 0..1).

    Purpose-built per query class, not a global authority score: a trusted-domain
    boost is only correct for information queries, which is all this engine serves.
    Spam/ads classification lives in reputation.py (AdGuard lists + local overrides);
    this function only encodes domain *shape* and known-good sources.
    """
    prior = 0.0
    penalty = 0.0
    path = urlsplit(url).path.lower()
    is_ugc = (host.startswith(UGC_PREFIXES)
              # github.com is documentation when it is a repo or a file, and UGC when
              # it is an issue thread, a pull request or a discussion
              or (host in ("github.com", "www.github.com")
                  and bool(GITHUB_UGC_RE.search(path))))
    trusted = (any(host == s or host.endswith("." + s) for s in TRUSTED_SUFFIXES)
               or any(host == s or host.endswith("." + s) for s in VENDOR_HOSTS))
    if trusted and not is_ugc:
        prior += 0.8
    elif trusted and is_ugc:
        prior += 0.15          # forums under a trusted domain: useful, not authoritative
    if any(host == s or host.endswith("." + s) for s in SOCIAL_HOSTS):
        prior += 0.12
    reputable = any(host == s or host.endswith("." + s) for s in REPUTABLE_TECH)
    if intent.startswith("tech") and reputable:
        prior += 0.30
    storefront = bool(STOREFRONT_RE.match(f"{host}{path}"))
    if storefront:
        prior += 0.45
    small = smallweb.is_small(host)
    if small:
        # Kagi's curated personal-blog allowlist: an independent human site is
        # worth a small nudge over an anonymous commercial one
        prior += 0.15
    if (intent.startswith("tech") and choice
            and any(host == s or host.endswith("." + s) for s in GENERAL_REFERENCE)):
        # an encyclopedia is background reading for "which browser should I use",
        # not an answer, while it stays a good source for "what is a browser".
        # Subtracted from the +0.8 trusted-domain prior, so this lands near neutral.
        prior -= 0.90
    if host.endswith(DISCOUNTED_SUFFIXES):
        prior -= 0.25
    if host.count("-") >= 3:
        prior -= 0.15
    if NUMERIC_PATH_RE.search(urlsplit(url).path):
        prior -= 0.1
    # trust is for the SEO classifier: how much benefit of the doubt a roundup
    # on this host deserves. Kept independent of the query intent on purpose.
    if trusted and not is_ugc:
        trust = 1.0
    elif reputable:
        trust = 0.85
    elif trusted and is_ugc:
        trust = 0.50
    elif any(host == s or host.endswith("." + s) for s in SOCIAL_HOSTS):
        trust = 0.60
    elif storefront:
        trust = 0.90
    elif small:
        trust = 0.70          # curated human site: a roundup here is not SEO bait
    elif host.endswith(DISCOUNTED_SUFFIXES):
        trust = 0.15
    else:
        trust = 0.35
    return max(-1.0, min(1.0, prior)), penalty, trust


# ------------------------------------------------------------------- freshness
def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d",
                "%B %d, %Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def friction_score(age_days: float, half_life: float) -> float:
    """Exponential decay, 1.0 today, 0.5 at one half-life."""
    return math.exp(-math.log(2) * max(0.0, age_days) / max(1.0, half_life))


# --------------------------------------------------------------------- scoring
def score_hit(hit: Any, query: str, terms: list[str], intent: str,
              max_rrf: float, cfg: dict[str, Any], rep: Any = None) -> Ranked:
    w = cfg["weights"]
    pen = cfg["penalties"]
    choice = is_choice_query(query, terms)
    f = Features(intent=intent)
    hit_score = float(getattr(hit, "score", 0.0) or 0.0)
    f.rrf = hit_score / max_rrf if max_rrf > 0 else 0.0
    sources = getattr(hit, "sources", []) or []
    best_rank = min((s.get("r", 99) for s in sources), default=99)
    f.rank = max(0.0, 1.0 - (best_rank - 1) / 20.0)
    f.agree = min(1.0, (len(sources) - 1) / 3.0) if sources else 0.0

    f.title = min(1.0, _title_match(terms, hit.title) + _phrase_bonus(query, hit.title))
    f.snippet = _coverage(terms, hit.snippet or "")

    prior, _shape_penalty, trust = _domain_prior(host_of(hit.url), hit.url, intent,
                                                 is_choice_query(query, terms))
    f.domain = prior
    # roundups: suspicious only when the publisher has no standing. A ZDNet
    # "best of" is an answer; the same title on an unknown host is a bid for traffic.
    seo = quality.seo_penalty(hit.title, hit.url, hit.snippet or "", trust)
    f.seo = seo.penalty
    f.penalties += pen.get("seo_listicle", 0.0) * seo.penalty
    if rep is not None and getattr(rep, "category", "none") in pen:
        f.penalties += pen[rep.category]
        f.reputation = rep.category

    published = _parse_date(getattr(hit, "published", None))
    half_life = 180.0 if intent.startswith("tech") else cfg["fresh_half_life_days"]
    if published:
        days = (datetime.now(timezone.utc) - published).days
        f.published_days = days
        f.fresh = friction_score(days, half_life)
        years = _years_in(query)
        if years and published.year < max(years) - 1:
            f.penalties += pen["stale_for_year"]
        if intent.startswith("tech"):
            # an undated page is not punished: engine dates are often the first
            # publication, and plenty of current reference pages carry none
            if days > 1080:
                f.penalties += pen["tech_stale_3y"]
            elif days > 540:
                f.penalties += pen["tech_stale_18m"]
    else:
        f.fresh = 0.0

    # The page's own claim beats engine metadata: an article titled "... in 2024"
    # is last-generation for a current choice query, whatever date the engine
    # reports (and engines often report the first publication of a live page).
    years_in_query = _years_in(query)
    if intent.startswith("tech") and choice and not years_in_query:
        current = datetime.now(timezone.utc).year
        # the page's own claim: title first, then the slug, because listicles often
        # carry the year only in the URL (/best-android-browsers-2024/)
        claimed = _years_in(hit.title or "") or _years_in(urlsplit(hit.url).path)
        if claimed:
            newest = max(claimed)
            if newest <= current - 2:
                f.penalties += pen["title_year_old"]
            elif newest == current - 1:
                f.penalties += pen["title_year_last"]

    snip_len = len(hit.snippet or "")
    f.quality = 1.0 if 60 <= snip_len <= 400 else (0.4 if snip_len else 0.0)
    if snip_len and snip_len < 60:
        f.penalties += pen["thin_snippet"]

    title_len = len(hit.title or "")
    if title_len < 15:
        f.penalties += pen["short_title"]
    if not (hit.title or "").strip():
        f.penalties += pen["empty_title"]

    # Currency matters when the query asks which software to use, and hardly at all
    # when it asks how an API works: an old man page is still correct, an old
    # "best browsers" list is a different answer. Same intent, different weight.
    if intent == "recency":
        fresh_weight = w["fresh"]
    elif intent == "tech_recency":
        fresh_weight = w.get("fresh_tech", 0.10) * 1.25
    elif intent == "tech" and choice:
        fresh_weight = w.get("fresh_tech", 0.10)
    else:
        fresh_weight = w["fresh"] * 0.2
    score = (w["rrf"] * f.rrf
             + w["rank"] * f.rank
             + w["agree"] * f.agree
             + w["title"] * f.title
             + w["snippet"] * f.snippet
             + w["domain"] * f.domain
             + fresh_weight * f.fresh
             + w["quality"] * f.quality
             - f.penalties)
    return Ranked(hit=hit, score=score, features=f)


def diversify(ranked: list[Ranked], max_per_domain: int) -> list[Ranked]:
    """Greedy per-domain cap, overflow appended in score order.

    Pure score ordering lets one site own the first screen (five Phoronix links for
    a Fedora query), which is technically relevant and practically useless.
    """
    if max_per_domain <= 0:
        return ranked
    seen: dict[str, int] = {}
    primary: list[Ranked] = []
    deferred: list[Ranked] = []
    for item in ranked:
        host = host_of(item.hit.url)
        if seen.get(host, 0) < max_per_domain:
            primary.append(item)
            seen[host] = seen.get(host, 0) + 1
        else:
            deferred.append(item)
    return primary + deferred


def rank(hits: list[Any], query: str, *, config: dict[str, Any] | None = None,
         diversify_results: bool = True,
         reputation: dict[str, Any] | None = None) -> list[Ranked]:
    cfg = config or load_config()
    terms = query_terms(query)
    intent = detect_intent(query)
    reputation = reputation or {}
    max_rrf = max((float(getattr(h, "score", 0.0) or 0.0) for h in hits), default=0.0)
    scored = [score_hit(h, query, terms, intent, max_rrf, cfg,
                        rep=reputation.get(h.domain)) for h in hits]
    scored.sort(key=lambda r: -r.score)
    if diversify_results:
        scored = diversify(scored, int(cfg["diversify_max_per_domain"]))
    return scored
