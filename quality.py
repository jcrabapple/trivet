"""Detect SEO-shaped pages: listicles, doorway slugs, affiliate boilerplate.

The hard part is not spotting a listicle, it is spotting the *bad* one. "The 9 best
Android browsers, tested by experts" from ZDNet is the answer a user wants; "Top 10
Best Android Browsers 2026 (Ranked & Reviewed)" from an unheard-of domain is the
page that exists only to rank. So the signal is the interaction:

    penalty = listicle_markers × (1 − publisher_trust) × structural_smell

Publisher trust comes from the ranking layer's host tiers, which already know
reference docs (1.0), established tech publishers (0.85), community sites (0.6),
unknown hosts (0.35) and discounted TLDs.

Nothing here fetches a page. Everything is computable from the result the engines
already returned, so it costs microseconds and works on every candidate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# "10 Best X in 2026", "Top 5 VPNs", "The 9 best browsers, tested"
LISTICLE_RE = re.compile(
    r"\b(\d{1,2}\s+(best|top|greatest|must-have|essential)|"
    r"(best|top)\s+\d{1,2}\b|"
    r"\b(top|best)\s+\w+(\s+\w+){0,4}\s+(of|for|in)\s+(20\d\d|android|ios|linux|windows|mac)|"
    r"\b(best|top)\s+\w+(\s+\w+){0,3}\b)",
    re.I)
YEAR_IN_TITLE_RE = re.compile(r"\b20\d\d\b")
RANKING_WORDS_RE = re.compile(r"\b(ranked|reviewed|comparison|picks|roundup|tested|compared)\b", re.I)
GUIDE_SPAM_RE = re.compile(
    r"\b(ultimate|complete|definitive|everything you need to know|"
    r"the only \w+ (you need|guide)|beginners? guide|cheat sheet|"
    r"buying guide|best picks|expert picks|we tested)\b", re.I)
AFFILIATE_RE = re.compile(
    r"\b(we may earn|affiliate|commission|sponsored|partner links|"
    r"our team (tested|reviewed)|expert(s)? tested|updated (for|on) 20\d\d)\b", re.I)

SLUG_YEAR_RE = re.compile(r"\b20\d\d\b")


@dataclass
class SeoFeatures:
    listicle: float = 0.0        # 0..1 markers in the title
    structure: float = 0.0       # 0..1 URL-shape smell
    boilerplate: float = 0.0     # 0..1 affiliate/roundup language in the snippet
    penalty: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


def listicle_score(title: str) -> float:
    """How strongly the title reads as a ranked roundup."""
    if not title:
        return 0.0
    score = 0.0
    if LISTICLE_RE.search(title):
        score += 0.55
    if YEAR_IN_TITLE_RE.search(title):
        score += 0.20
    if RANKING_WORDS_RE.search(title):
        score += 0.15
    if GUIDE_SPAM_RE.search(title):
        score += 0.25
    return min(1.0, score)


def structure_score(url: str) -> float:
    """URL shape that roundups-for-hire share: deep dated blog paths, long
    keyword slugs with a year, repeated separators."""
    parts = urlsplit(url)
    path = parts.path.lower().strip("/")
    segments = [s for s in path.split("/") if s]
    score = 0.0
    if len(segments) >= 3:
        score += 0.25
    slug = segments[-1] if segments else ""
    slug_words = [w for w in re.split(r"[-_]", slug) if w]
    if len(slug_words) >= 6:
        score += 0.30
    elif len(slug_words) >= 4:
        score += 0.15
    if SLUG_YEAR_RE.search(slug):
        score += 0.20
    if any(s in ("blog", "news", "posts", "article") for s in segments[:-1]):
        score += 0.15
    if re.search(r"[-_]?(ranked|reviewed|top-?\d+|best-?\d+)", slug):
        score += 0.20
    return min(1.0, score)


def boilerplate_score(snippet: str, title: str) -> float:
    """Roundup/affiliate language that only appears on commercial listicles."""
    text = f"{title} {snippet}"
    if not text.strip():
        return 0.0
    hits = len(AFFILIATE_RE.findall(text))
    return min(1.0, hits * 0.4)


SLUG_LISTICLE_RE = re.compile(r"(best|top|ranked|reviewed|comparison|vs)[-_]", re.I)


def seo_penalty(title: str, url: str, snippet: str, publisher_trust: float) -> SeoFeatures:
    """Combine markers with publisher trust.

    A perfect listicle from a reference source scores 0; a perfect listicle from an
    unknown host scores close to the configured weight.
    """
    f = SeoFeatures(
        listicle=listicle_score(title),
        structure=structure_score(url),
        boilerplate=boilerplate_score(snippet or "", title or ""),
    )
    # strip the trailing slash first: "/blogs/best-android-browsers/" otherwise
    # yields an empty final segment and the rule silently never fires
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    if f.listicle < 0.2 and SLUG_LISTICLE_RE.search(slug):
        # the title is generic ("Web Browsers for Android") but the URL says
        # /blogs/best-android-browsers-2025/: the page itself knows what it is
        f.listicle = 0.5
    if f.listicle < 0.2:
        f.structure = 0.0        # a plain doc page with a long path is not a listicle
        f.boilerplate = 0.0
    smell = (0.6 * f.listicle + 0.25 * f.structure + 0.15 * f.boilerplate)
    f.penalty = max(0.0, min(1.0, smell * (1.0 - publisher_trust)))
    return f
