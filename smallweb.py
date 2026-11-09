"""Kagi Small Web as a positive domain prior.

Our ranker already punishes SEO and rewards established publishers, but it had no
concept of "independent human site worth surfacing" — the half of Kagi's stated
policy ("promote content from independent, ad-free sources and personal websites")
that we were missing.

Kagi publishes the list under MIT at kagisearch/smallweb: ~43k personal-blog
domains, updated continuously, with an OPML file and an Atom feed. It is a curated
allowlist, so membership is a weak positive signal, not a guarantee: +0.15 on the
domain prior and a mid trust tier for the SEO classifier.
"""
from __future__ import annotations

import os
import time
import urllib.request
from urllib.parse import urlsplit

URL = "https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt"
PATH = os.path.expanduser("~/.cache/trivet/smallweb.txt")
MAX_AGE_DAYS = 7

_state: dict[str, object] = {"mtime": 0.0, "domains": frozenset()}


def refresh(timeout: int = 30) -> int:
    """Download the list. Returns the number of domains, 0 on failure."""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    try:
        with urllib.request.urlopen(URL, timeout=timeout) as resp, open(tmp, "wb") as fh:
            fh.write(resp.read())
        os.replace(tmp, PATH)
    except Exception:  # noqa: BLE001 - a stale list beats no list
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    _state["mtime"] = 0.0        # force a reload on next use
    return len(_load())


def _load() -> frozenset[str]:
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        return frozenset()
    if _state["mtime"] == mtime and _state["domains"]:
        return _state["domains"]  # type: ignore[return-value]
    domains: set[str] = set()
    try:
        with open(PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                host = urlsplit(line.split()[0]).netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    domains.add(host)
    except OSError:
        return frozenset()
    _state["mtime"] = mtime
    _state["domains"] = frozenset(domains)
    return _state["domains"]  # type: ignore[return-value]


def ensure(max_age_days: int = MAX_AGE_DAYS) -> None:
    """Refresh when the local copy is missing or stale. Called at startup."""
    try:
        age_days = (time.time() - os.path.getmtime(PATH)) / 86400
    except OSError:
        age_days = 1e9
    if age_days > max_age_days:
        refresh()


def is_small(host: str) -> bool:
    """Membership test. Exact host only: subdomains are separate sites."""
    domains = _load()
    if not domains:
        return False
    return host in domains


def size() -> int:
    return len(_load())
