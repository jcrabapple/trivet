"""Ranking: turn fused engine results into an ordered list with explainable signals.

Stage 1 (merge.py) fuses engine ranks with RRF, which captures consensus and
nothing else: it never looks at the query, the title, the source, or the date.
That is why a 2022 article can outrank this month's release notes on a "best X
2026" query, or a verbose forum question can outrank canonical API docs.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

import providers
import quality
import smallweb

CONFIG_PATH = os.path.expanduser("~/.config/trivet/ranking.json")

DEFAULT_CONFIG: dict[str, Any] = {
    # Tuned against eval/queries.json (22 queries), verified on eval/holdout.json
    # (4 unseen queries) and a 10-query cross-domain probe set. Re-tune against a
    # frozen snapshot after adding queries, never against live refetches.
    "weights": {
        "rrf": 0.25,
        "rank": 0.10,
        "agree": 0.04,
        "title": 0.10,
        "snippet": 0.05,
        "domain": 0.22,
        "fresh": 0.05,
        # 0.0 on purpose: a positive recency weight promoted whoever re-dates a
        # listicle, and measured worse on both the doc queries and the review
        # queries. Staleness is handled by the penalties below instead.
        "fresh_tech": 0.0,
        "quality": 0.05,
    },
    "penalties": {
        "empty_title": 0.20,
        "short_title": 0.08,       # raw-URL titles or <6-char unmatched stubs
        "stale_for_year": 0.12,    # query names a year, page is older
        "tech_stale_18m": 0.20,    # tech: 18 months old is a different answer
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
    r"ubuntu|debian|gentoo|opensuse|arch linux|wayland|x11|hyprland|sway|"
    r"browser|browsers|app|apps|software|sdk|api|framework|library|plugin|"
    r"extension|driver|firmware|gpu|cpu|ram|ssd|nvme|laptop|router|nas|"
    r"docker|dockerfile|podman|quadlet|kubernetes|k8s|python|rust|golang|javascript|"
    r"typescript|react|vue|svelte|node|npm|deno|sql|postgres|postgresql|"
    r"mysql|mariadb|sqlite|sqlite3|redis|nginx|apache|caddy|cli|terminal|bash|zsh|"
    r"shell|git|ssh|systemd|cron|vpn|dns|codec|programming|developer|coding|code|"
    r"ai|llm|ml|agent|agents|self-hosted|selfhosted|homelab|container|containers|"
    r"package|database|fastapi|django|flask|"
    r"compile|kernel|distro|flatpak|snap|esp32|esp8266|arduino|raspberry pi|"
    r"app store|play store)\b",
    re.I)

# Established editorial reviewers (tech and general product testing).
REPUTABLE_REVIEWERS = (
    "zdnet.com", "androidauthority.com", "howtogeek.com", "tomsguide.com",
    "tomshardware.com", "pcmag.com", "arstechnica.com", "theverge.com", "wired.com",
    "engadget.com", "cnet.com", "techradar.com", "xda-developers.com",
    "androidpolice.com", "9to5google.com", "androidcentral.com", "ghacks.net",
    "bleepingcomputer.com", "phoronix.com", "lwn.net", "theregister.com",
    "rtings.com", "slashgear.com", "digitaltrends.com", "thewirecutter.com",
    "consumerreports.org", "popularmechanics.com", "seriouseats.com",
    "kingarthurbaking.com", "theperfectloaf.com", "outdoorgearlab.com",
)
# Kept as an alias so any external caller inspecting REPUTABLE_TECH keeps working.
REPUTABLE_TECH = REPUTABLE_REVIEWERS

MAJOR_NEWS_EDITORIAL = (
    "nytimes.com", "theguardian.com", "bbc.com", "bbc.co.uk", "reuters.com",
    "apnews.com", "npr.org",
)

# Official app/extension storefronts. On specific tool queries ("sync bookmarks
# extension chrome") the listing IS the answer; on broad choice queries ("Android
# Web browsers") one random app's store page should not beat multi-app reviews.
STOREFRONT_RE = re.compile(
    r"^(chromewebstore\.google\.com/detail/"
    r"|chrome\.google\.com/webstore/detail/"
    r"|addons\.mozilla\.org/[a-z-]+/firefox/addon/"
    r"|microsoftedge\.microsoft\.com/addons/detail/"
    r"|apps\.apple\.com/[a-z]{2}/app/"
    r"|play\.google\.com/store/apps/details)", re.I)

# github.com / gitlab.com path tiers: canonical 2-segment repo roots are primary
# project pages; /blob/ files inside random repos and /issues/ threads are not.
GITHUB_UGC_RE = re.compile(
    r"/(issues|pull|pulls|discussions|search|sponsors|gist)(/|$)", re.I)
GITHUB_FILE_RE = re.compile(
    r"^/[^/]+/[^/]+/(blob|tree|raw|commits|blame|releases)(/|$)", re.I)

# A *choice* query wants current options, not an encyclopedia concept or SDK docs.
CHOICE_RE = re.compile(
    r"\b(best|top|vs|versus|which|compare|comparison|alternative|alternatives|"
    r"recommend|recommended|should i|worth it)\b", re.I)

GENERAL_REFERENCE = ("wikipedia.org", "britannica.com", "encyclopedia.com",
                     "dictionary.com", "merriam-webster.com", "wikidata.org")

RECENCY_RE = re.compile(
    r"\b(20[2-9]\d|latest|newest|new|news|today|tonight|current|"
    r"updated?|changelog|announcement|roadmap)\b", re.I)
FACT_RE = re.compile(r"\b(date|history|historically|when|timeline|since)\b", re.I)
YEAR_RE = re.compile(r"\b(20[2-9]\d)\b")

# Universal civic, academic, standards, clinical, and primary language/OS manuals.
PRIMARY_REFERENCE = (
    ".gov", ".edu", ".mil", ".int",
    "wikipedia.org", "wikimedia.org", "arxiv.org", "acm.org", "ieee.org",
    "rfc-editor.org", "ietf.org", "w3.org", "unicode.org", "man7.org", "ss64.com",
    "wiki.archlinux.org", "archlinux.org", "debian.org",
    "fedoraproject.org", "docs.fedoraproject.org",
    "docs.python.org", "python.org", "developer.mozilla.org", "mozilla.org",
    "kernel.org", "rust-lang.org", "doc.rust-lang.org", "go.dev", "nodejs.org",
    "postgresql.org", "sqlite.org", "nginx.org", "docs.nginx.com", "openssl.org",
    "kubernetes.io", "docs.docker.com", "podman.io", "docs.podman.io",
    "systemd.io", "freedesktop.org", "docs.espressif.com",
    # Non-profit clinical / scientific reference institutions
    "mayoclinic.org", "clevelandclinic.org", "hopkinsmedicine.org",
    "sleepfoundation.org", "cancer.org", "mdanderson.org",
    "allaboutbirds.org", "audubon.org",
)
# Kept as an alias for backwards compatibility.
TRUSTED_SUFFIXES = PRIMARY_REFERENCE

# Vendor documentation and blogs: authoritative for their own ecosystem, ordinary
# corporate content when blogging off-topic (e.g. blog.cloudflare.com on AI tools).
VENDOR_ECOSYSTEMS = (
    (("redhat.com", "docs.redhat.com", "developers.redhat.com", "fedoramagazine.org"),
     re.compile(r"\b(redhat|red hat|rhel|fedora|centos|podman|quadlet|ansible|openshift|selinux)\b", re.I)),
    (("cloudflare.com", "developers.cloudflare.com", "blog.cloudflare.com"),
     re.compile(r"\b(cloudflare|cloudflared|workers|wrangler|r2|pages|tunnel|warp)\b", re.I)),
    (("learn.microsoft.com", "support.microsoft.com"),
     re.compile(r"\b(microsoft|windows|azure|dotnet|c#|powershell|bitlocker|wsl|vscode|active directory|ntfs|hyper-v)\b", re.I)),
    (("developer.apple.com", "support.apple.com"),
     re.compile(r"\b(apple|mac|macos|ios|ipados|iphone|ipad|swift|swiftui|xcode|airpods|icloud)\b", re.I)),
    (("developer.android.com", "source.android.com"),
     re.compile(r"\b(android|kotlin|jetpack|gradle|adb|apk|webview|ndk|aosp)\b", re.I)),
)

PACKAGE_REGISTRIES = (
    "pypi.org", "crates.io", "docs.rs", "pkg.go.dev", "npmjs.com",
    "rubygems.org", "packagist.org", "flathub.org", "hub.docker.com",
)

# Curated technical Q&A: strong for exact error strings, deliberately below
# primary reference docs (+0.80) so verbose question titles do not displace
# canonical API manuals.
QA_HOSTS = (
    "stackoverflow.com", "serverfault.com", "superuser.com",
    "askubuntu.com", "stackexchange.com",
)

# Subdomains and paths of a trusted domain that host user discussion or personal
# course mirrors rather than canonical reference text.
UGC_PREFIXES = ("discussion.", "community.", "forums.", "forum.", "packages.",
                "ask.", "answers.", "users.", "bugs.", "issues.", "gitlab.",
                "lists.", "mail.", "old.", "beta.",
                "gist.", "snippets.", "paste.")
EDU_PERSONAL_PREFIXES = ("web.", "people.", "personal.", "users.", "student.",
                         "students.", "courses.", "classes.")
PATH_UGC_RE = re.compile(
    r"(^|/)(answers|questions|forums?|forum|discussions?|t|community/questions)(/|$)",
    re.I)

DOC_SUBDOMAINS = ("docs.", "doc.", "wiki.", "help.", "manual.", "reference.")
DOC_PATH_RE = re.compile(
    r"^/(docs?|documentation|reference|manual|tutorial|tutorials|guide|guides|"
    r"handbook|man|api|book|en/latest|en/stable|getting-started)(/|$)", re.I)
BLOG_PATH_RE = re.compile(r"(^|/)(blog|blogs|articles|news|sysadmin|posts)(/|$)", re.I)

VERSIONED_DOC_RE = re.compile(r"/(v?\d+\.\d+(?:\.\d+)?|r\d+|rust-lang_v[\d.]+)(/|$)", re.I)
CANONICAL_VER_RE = re.compile(r"/(latest|current|stable)(/|$)", re.I)

# Common generic words excluded from subject-host brand matching so exact-match SEO
# domains (thebestvpn.com, sqldocs.org, parkstatus.today) never look like official
# project homepages.
GENERIC_HOST_WORDS = {
    "best", "top", "web", "app", "apps", "tool", "tools", "code", "coding", "dev",
    "tech", "vpn", "sql", "database", "linux", "android", "windows", "mac", "ios",
    "cloud", "blog", "news", "guide", "guides", "docs", "doc", "wiki", "help",
    "free", "online", "pro", "hub", "lab", "home", "park", "drive", "status",
    "sleep", "health", "daily", "dose", "ai", "review", "reviews", "compare",
    "test", "open", "source", "server", "client", "manager", "package", "system",
    "container", "network", "data", "learn", "howto", "tips", "answers", "info",
}

DISCOUNTED_SUFFIXES = (
    "pinterest.com", "quora.com", "answers.com", "ehow.com", "wikihow.com",
    ".info", ".click", ".top", ".buzz", ".loan", ".win",
)
SOCIAL_HOSTS = ("reddit.com", "news.ycombinator.com", "lobste.rs")


def load_config() -> dict[str, Any]:
    """Default weights, overlaid by ~/.config/trivet/ranking.json if present."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
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
def _split_punct(text: str) -> str:
    """Keep version decimals (3.14), split identifier/extension dots and separators."""
    s = text.lower().replace("_", " ").replace("-", " ").replace("/", " ")
    return re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", s)


def _word_tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+#.]*", _split_punct(text)) if w]


def query_terms(query: str) -> list[str]:
    return [t for t in _word_tokens(query) if t not in STOPWORDS and len(t) > 1]


def detect_intent(query: str) -> str:
    tech = bool(TECH_RE.search(query))
    if tech:
        if FACT_RE.search(query):
            return "tech"
        return "tech_recency" if RECENCY_RE.search(query) else "tech"
    if FACT_RE.search(query):
        return "general"
    return "recency" if RECENCY_RE.search(query) else "general"


def _years_in(query: str) -> list[int]:
    return [int(y) for y in YEAR_RE.findall(query)]


def _term_hits(term: str, words: list[str], word_set: set[str]) -> bool:
    """Word-boundary match with symmetric plural/stem tolerance for >=4-char words.

    Short 2-3 char tokens ('ai', 'uv', 'go', 'os') require exact whole-word match so
    'ai' never matches 'said' or 'domain'. Longer words match small inflectional
    suffixes in both directions ('kidneys' <-> 'kidney', 'addition' <-> 'additions').
    """
    if term in word_set:
        return True
    if len(term) >= 4:
        for w in words:
            if (w.startswith(term) or (len(w) >= 4 and term.startswith(w))) and abs(len(w) - len(term)) <= 3:
                return True
    return False


def _coverage(terms: list[str], text: str) -> float:
    if not terms or not text:
        return 0.0
    words = _word_tokens(text)
    if not words:
        return 0.0
    wset = set(words)
    low = text.lower()
    hits = 0.0
    for t in terms:
        if _term_hits(t, words, wset):
            hits += 1.0
        elif len(t) >= 4 and t in low:
            hits += 0.85          # glued compounds (e.g. coderabbit -> code)
    return min(1.0, hits / len(terms))


def _title_match(terms: list[str], title: str, url: str = "") -> float:
    """Coverage, rewarded for covering everything, backed by URL slug alignment."""
    if not terms:
        return 0.0
    coverage = _coverage(terms, title)
    tokens = len(_word_tokens(title))
    slack = max(0, tokens - 2 * len(terms))
    density = max(0.5, 1.0 / (1.0 + slack / 12.0))
    complete = 1.0 if coverage >= 0.999 else 0.0
    base = (0.65 * coverage + 0.35 * complete) * density
    if url:
        parts = urlsplit(url)
        slug_cov = _coverage(terms, f"{parts.netloc} {parts.path}")
        base += 0.16 * slug_cov
    return min(1.0, base)


def _phrase_bonus(query: str, text: str) -> float:
    q = " ".join(query.lower().split())
    return 0.25 if len(q) > 6 and q in " ".join(text.lower().split()) else 0.0


# --------------------------------------------------------------- domain prior
def host_of(url: str) -> str:
    host = (urlsplit(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


def _host_is(host: str, suffixes: Iterable[str]) -> bool:
    for s in suffixes:
        sc = s.lstrip(".")
        if host == sc or host.endswith("." + sc):
            return True
    return False


CATEGORY_NOUN_RE = re.compile(
    r"\b(browsers|apps|clients|tools|editors|players|managers|distros|suites|"
    r"ides|servers|extensions|plugins|themes|launchers|keyboards|mice|laptops)\b",
    re.I)


def is_choice_query(query: str, terms: list[str] | None = None) -> bool:
    """A pick-my-option query: explicit choice words, or a tech category-noun query."""
    if CHOICE_RE.search(query):
        return True
    return bool(TECH_RE.search(query) and CATEGORY_NOUN_RE.search(query))


def _host_brand_tokens(host: str) -> set[str]:
    h = host.lower()
    for p in ("www.", "docs.", "doc.", "wiki.", "help.", "developer.",
              "developers.", "learn.", "support.", "blog."):
        if h.startswith(p):
            h = h[len(p):]
    for multi in (".github.io", ".readthedocs.io", ".co.uk", ".org.uk"):
        if h.endswith(multi):
            h = h[:-len(multi)]
            return {tok for tok in re.split(r"[.\-]", h) if len(tok) >= 2}
    labels = h.split(".")
    stem = labels[:-1] if len(labels) > 1 else labels
    return {tok for part in stem for tok in part.split("-") if len(tok) >= 2}


def _subject_host_match(host: str, path: str, terms: list[str]) -> tuple[bool, bool]:
    """Detect when the candidate's domain (or docs.* root path) owns the queried entity."""
    is_doc_host = host.startswith(DOC_SUBDOMAINS) or host.endswith(".readthedocs.io")
    is_doc_path = bool(DOC_PATH_RE.search(path))
    distinctive = [t for t in terms
                   if len(t) >= 3 and t not in GENERIC_HOST_WORDS and not t.isdigit()]
    short_distinctive = [t for t in terms
                         if len(t) == 2 and t not in GENERIC_HOST_WORDS and not t.isdigit()]
    brands = _host_brand_tokens(host)
    brand_hit = any(t in brands for t in distinctive)
    segs = [s for s in path.lower().strip("/").split("/") if s]
    first_seg = segs[0] if segs else ""
    doc_seg_hit = is_doc_host and bool(first_seg) and (
        first_seg in distinctive or first_seg in short_distinctive)
    return (brand_hit or doc_seg_hit), (is_doc_host or is_doc_path)


def _domain_prior(host: str, url: str, query: str = "", terms: list[str] | None = None,
                  intent: str = "general", choice: bool = False,
                  agreement: int = 1) -> tuple[float, float, float]:
    """Returns (prior in -1..1, shape_penalty, publisher_trust in 0..1)."""
    terms = terms or []
    prior = 0.0
    penalty = 0.0
    path = urlsplit(url).path.lower()
    is_sub_ugc = host.startswith(UGC_PREFIXES)
    is_path_ugc = bool(PATH_UGC_RE.search(path))
    is_edu_personal = host.endswith(".edu") and (
        host.startswith(EDU_PERSONAL_PREFIXES) or "/~" in path)

    if host in ("github.com", "gitlab.com"):
        if GITHUB_UGC_RE.search(path) or is_sub_ugc:
            return 0.15, 0.0, 0.55
        if GITHUB_FILE_RE.search(path):
            return 0.18, 0.0, 0.60
        segs = [s for s in path.strip("/").split("/") if s]
        distinctive = {t for t in terms
                       if len(t) >= 2 and t not in GENERIC_HOST_WORDS and not t.isdigit()}
        repo_match = bool(segs) and any(s.lower() in distinctive for s in segs[:2])
        return (0.62 if (repo_match and agreement >= 2) else 0.42), 0.0, 0.85

    primary = _host_is(host, PRIMARY_REFERENCE) and host != "pypi.python.org"
    vendor_match = False
    vendor_any = False
    for v_hosts, v_re in VENDOR_ECOSYSTEMS:
        if _host_is(host, v_hosts):
            vendor_any = True
            vendor_match = bool(v_re.search(query))
            break

    is_blog = host.startswith("blog.") or bool(BLOG_PATH_RE.search(path))
    is_ugc = is_sub_ugc or is_path_ugc
    subj_match, is_doc_surface = _subject_host_match(host, path, terms)
    is_doc_subdomain = host.startswith(DOC_SUBDOMAINS) or host.endswith(".readthedocs.io")

    if primary:
        if is_ugc or is_edu_personal:
            prior += 0.22
            trust = 0.65
        else:
            prior += 0.80
            if subj_match:
                prior += 0.08     # project's own primary domain beats a generic mirror
            trust = 1.0
    elif vendor_any:
        if is_ugc:
            prior += 0.22
            trust = 0.60
        elif choice and not vendor_match:
            prior += 0.0
            trust = 0.80
        elif vendor_match:
            prior += 0.65 if is_blog else 0.78
            trust = 0.95
        else:
            prior += 0.15 if is_blog else 0.35
            trust = 0.85
    elif _host_is(host, QA_HOSTS):
        prior += 0.28
        trust = 0.80
    elif _host_is(host, PACKAGE_REGISTRIES):
        prior += 0.42
        trust = 0.90
    elif host == "pypi.python.org":
        prior += 0.20
        trust = 0.80
    else:
        trust = 0.35
        if subj_match and is_doc_surface and not is_ugc:
            prior += 0.72
            trust = 0.95
        elif subj_match and not is_ugc:
            prior += 0.45
            trust = 0.85
        elif is_doc_subdomain and not is_ugc:
            prior += 0.40
            trust = 0.80

    # Canonical /latest/ or /current/ beats pinned archive versions unless a version was asked for.
    if (primary or is_doc_surface or is_doc_subdomain) and not re.search(r"\b\d+\.\d+\b", query):
        if CANONICAL_VER_RE.search(path):
            prior += 0.05
        elif VERSIONED_DOC_RE.search(path):
            prior -= 0.08

    is_wirecutter = host == "nytimes.com" and path.startswith("/wirecutter")
    reputable = _host_is(host, REPUTABLE_REVIEWERS) or is_wirecutter
    if reputable and (intent.startswith("tech") or choice):
        prior += 0.32
    if reputable:
        trust = max(trust, 0.85)
    elif _host_is(host, MAJOR_NEWS_EDITORIAL):
        trust = max(trust, 0.75)

    if _host_is(host, SOCIAL_HOSTS):
        prior += 0.15
        trust = max(trust, 0.65)

    storefront = bool(STOREFRONT_RE.match(f"{host}{path}"))
    if storefront:
        if not choice:
            prior += 0.45
        trust = max(trust, 0.90)

    if smallweb.is_small(host):
        prior += 0.15
        trust = max(trust, 0.70)

    if choice and _host_is(host, GENERAL_REFERENCE):
        prior -= 0.90

    if _host_is(host, DISCOUNTED_SUFFIXES):
        prior -= 0.25
        trust = min(trust, 0.15)
    if host.count("-") >= 3:
        prior -= 0.15

    return max(-1.0, min(1.0, prior)), penalty, trust


# ------------------------------------------------------------------- freshness
def friction_score(age_days: float, half_life: float) -> float:
    """Exponential decay, 1.0 today, 0.5 at one half-life."""
    return math.exp(-math.log(2) * max(0.0, age_days) / max(1.0, half_life))


# --------------------------------------------------------------------- scoring
def score_hit(hit: Any, query: str, terms: list[str], intent: str,
              max_rrf: float, cfg: dict[str, Any], rep: Any = None,
              *, multi_engine_pool: bool = True) -> Ranked:
    w = cfg["weights"]
    pen = cfg["penalties"]
    choice = is_choice_query(query, terms)
    f = Features(intent=intent)
    hit_score = float(getattr(hit, "score", 0.0) or 0.0)
    f.rrf = hit_score / max_rrf if max_rrf > 0 else 0.0
    sources = getattr(hit, "sources", []) or []
    best_rank = min((s.get("r", 99) for s in sources), default=99)
    raw_rank = max(0.0, 1.0 - (best_rank - 1) / 20.0)
    if multi_engine_pool and len(sources) <= 1:
        raw_rank *= 0.65
    f.rank = raw_rank
    f.agree = min(1.0, (len(sources) - 1) / 3.0) if sources else 0.0

    f.title = min(1.0, _title_match(terms, hit.title, hit.url) + _phrase_bonus(query, hit.title))
    f.snippet = _coverage(terms, hit.snippet or "")

    prior, _shape_penalty, trust = _domain_prior(
        host_of(hit.url), hit.url, query=query, terms=terms,
        intent=intent, choice=choice, agreement=len(sources))
    f.domain = prior

    seo = quality.seo_penalty(hit.title, hit.url, hit.snippet or "", trust)
    f.seo = seo.penalty
    f.penalties += pen.get("seo_listicle", 0.0) * seo.penalty
    if rep is not None and getattr(rep, "category", "none") in pen:
        f.penalties += pen[rep.category]
        f.reputation = rep.category

    now_dt = datetime.now(timezone.utc)
    current_year = now_dt.year
    years_in_query = _years_in(query)
    claimed = _years_in(hit.title or "") or _years_in(urlsplit(hit.url).path)
    claimed_current = bool(claimed and max(claimed) >= current_year and trust >= 0.75)

    age_hours = providers.parse_age_hours(getattr(hit, "published", None),
                                          now=now_dt.timestamp())
    half_life = 180.0 if intent.startswith("tech") else cfg["fresh_half_life_days"]
    if age_hours is not None:
        days = int(age_hours // 24)
        f.published_days = days
        f.fresh = friction_score(days, half_life)
        if years_in_query and (current_year - days // 365) < max(years_in_query) - 1 and not claimed_current:
            f.penalties += pen["stale_for_year"]
        if intent.startswith("tech") and not claimed_current:
            if days > 1080:
                f.penalties += pen["tech_stale_3y"]
            elif days > 540:
                f.penalties += pen["tech_stale_18m"]
    else:
        f.fresh = 0.0

    if choice and not years_in_query and claimed:
        newest = max(claimed)
        if newest <= current_year - 2:
            f.penalties += pen["title_year_old"]
        elif newest == current_year - 1:
            f.penalties += pen["title_year_last"]

    snip_len = len(hit.snippet or "")
    f.quality = 1.0 if 60 <= snip_len <= 400 else (0.4 if snip_len else 0.0)
    if snip_len and snip_len < 60:
        f.penalties += pen["thin_snippet"]

    raw_title = (hit.title or "").strip()
    if not raw_title:
        f.penalties += pen["empty_title"]
    elif raw_title.lower().startswith(("http://", "https://")) or (
            len(raw_title) < 6 and _coverage(terms, raw_title) == 0):
        f.penalties += pen["short_title"]

    if intent == "recency":
        fresh_weight = w["fresh"]
    elif intent == "tech_recency":
        fresh_weight = w.get("fresh_tech", 0.0) * 1.25
    elif intent == "tech" and choice:
        fresh_weight = w.get("fresh_tech", 0.0)
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
    """Greedy per-domain cap (plus Q&A-network cap), overflow appended in score order."""
    if max_per_domain <= 0:
        return ranked
    seen: dict[str, int] = {}
    qa_seen = 0
    primary: list[Ranked] = []
    deferred: list[Ranked] = []
    for item in ranked:
        host = host_of(item.hit.url)
        is_qa = _host_is(host, QA_HOSTS)
        if seen.get(host, 0) < max_per_domain and (not is_qa or qa_seen < max_per_domain):
            primary.append(item)
            seen[host] = seen.get(host, 0) + 1
            if is_qa:
                qa_seen += 1
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
    multi_pool = any(len(getattr(h, "sources", []) or []) >= 2 for h in hits)
    scored = [score_hit(h, query, terms, intent, max_rrf, cfg,
                        rep=reputation.get(h.domain),
                        multi_engine_pool=multi_pool) for h in hits]
    scored.sort(key=lambda r: -r.score)
    if diversify_results:
        scored = diversify(scored, int(cfg["diversify_max_per_domain"]))
    return scored
