"""Bangs: `!gh rust ownership` jumps straight to GitHub's own search.

The definitions come from kagisearch/bangs (MIT), the same list Kagi uses, so the
triggers a user already knows work here. Two placements are accepted, matching how
people actually type: leading (`!w fedora`) and trailing (`fedora !w`).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from urllib.parse import quote_plus, urlsplit

URL = "https://raw.githubusercontent.com/kagisearch/bangs/main/data/bangs.json"
PATH = os.path.expanduser("~/.cache/trivet/bangs.json")
MAX_AGE_DAYS = 30          # triggers change slowly

_state: dict[str, object] = {"mtime": 0.0, "map": {}}


def refresh(timeout: int = 30) -> int:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    try:
        with urllib.request.urlopen(URL, timeout=timeout) as resp, open(tmp, "wb") as fh:
            fh.write(resp.read())
        os.replace(tmp, PATH)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    _state["mtime"] = 0.0
    return len(_load())


def _load() -> dict[str, dict]:
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        return {}
    if _state["mtime"] == mtime and _state["map"]:
        return _state["map"]  # type: ignore[return-value]
    table: dict[str, dict] = {}
    try:
        with open(PATH) as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        return {}
    for entry in entries:
        triggers = [entry.get("t") or ""] + list(entry.get("ts") or [])
        for trig in triggers:
            trig = trig.strip().lower()
            if trig and trig not in table:
                table[trig] = entry
    _state["mtime"] = mtime
    _state["map"] = table
    return table


def ensure(max_age_days: int = MAX_AGE_DAYS) -> None:
    try:
        age_days = (time.time() - os.path.getmtime(PATH)) / 86400
    except OSError:
        age_days = 1e9
    if age_days > max_age_days:
        refresh()


BANG_TOKEN = re.compile(r"(?:^|\s)!([a-z0-9][a-z0-9._-]{0,30})(?=\s|$)", re.I)


def resolve(query: str) -> dict | None:
    """Return {url, name, domain, trigger} when the query carries a known bang."""
    table = _load()
    if not table:
        return None
    match = BANG_TOKEN.search(query)
    if not match:
        return None
    trigger = match.group(1).lower()
    entry = table.get(trigger)
    if not entry:
        return None
    rest = (query[: match.start()] + " " + query[match.end():]).strip()
    template = entry.get("u") or ""
    if "{{{s}}}" in template:
        if rest:
            target = template.replace("{{{s}}}", quote_plus(rest))
        else:
            # no terms: land on the site itself rather than a search for nothing
            domain = entry.get("d") or ""
            target = f"https://{domain}" if domain else template.replace("{{{s}}}", "")
    else:
        target = template
    if not target.startswith(("http://", "https://")):
        return None
    return {"url": target, "name": entry.get("s") or trigger,
            "domain": entry.get("d") or urlsplit(target).netloc,
            "trigger": trigger}


def size() -> int:
    return len(_load())
