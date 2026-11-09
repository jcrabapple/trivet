"""Session cookies so the browser stops asking for Basic auth on every visit.

HTTP Basic has no logout, cannot be remembered reliably by every browser, and
re-prompts after a restart or in a private window. This module issues a signed
cookie instead:

    ms_session = base64(user|expiry) . hmac_sha256(secret, that string)

The secret lives in the unit's EnvironmentFile, so a restart does not invalidate
existing sessions, and a cookie cannot be forged without reading that file. Basic
auth stays supported for curl and scripts, which is what the generated API tokens
of the world would otherwise be for.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

COOKIE_NAME = "tv_session"
DEFAULT_DAYS = 14
MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60


SECRET_FILE = os.path.expanduser(
    os.environ.get("TRIVET_SESSION_SECRET_FILE", "~/.config/trivet/session-secret"))


def session_secret() -> str:
    """The cookie signing secret.

    Explicit configuration wins. Otherwise a secret is generated once and kept at
    `~/.config/trivet/session-secret` (0600) so a first install works and sessions
    survive a restart. The previous behaviour — empty secret, empty token, empty
    cookie — produced a login that redirected in a loop with no error anywhere.
    """
    configured = os.environ.get("TRIVET_SESSION_SECRET", "").strip()
    if configured:
        return configured
    try:
        with open(SECRET_FILE) as fh:
            existing = fh.read().strip()
            if existing:
                return existing
    except OSError:
        pass
    generated = secrets.token_urlsafe(48)
    try:
        os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(generated)
        print(f"WARNING: no TRIVET_SESSION_SECRET set; generated one at {SECRET_FILE}",
              file=__import__("sys").stderr, flush=True)
    except OSError:
        pass
    return generated


def session_days() -> int:
    try:
        return max(1, int(os.environ.get("TRIVET_SESSION_DAYS", DEFAULT_DAYS)))
    except ValueError:
        return DEFAULT_DAYS


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def issue_token(user: str, days: int | None = None) -> str:
    secret = session_secret()
    if not secret:
        return ""
    now = int(time.time())
    exp = now + (days or session_days()) * 86400
    payload = f"{user}|{exp}|{now}".encode()
    return f"{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{_sign(payload, secret)}"


def verify_token(token: str) -> str | None:
    """Return the user when the token is intact and unexpired, else None."""
    secret = session_secret()
    if not secret or not token or "." not in token:
        return None
    blob, _, sig = token.rpartition(".")
    try:
        padded = blob + "=" * (-len(blob) % 4)
        payload = base64.urlsafe_b64decode(padded.encode())
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(payload, secret), sig):
        return None
    try:
        user, exp, issued = payload.decode().rsplit("|", 2)
        if int(exp) < time.time():
            return None
        # TRIVET_SESSION_EPOCH: bump it to evict every existing session without
        # touching the secret that also protects stored API keys
        if int(issued) < int(os.environ.get("TRIVET_SESSION_EPOCH", "0") or 0):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return user


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------- login throttling
_failures: dict[str, list[float]] = {}
MAX_TRACKED_IPS = 4096


def failures_for(ip: str) -> int:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW]
    _failures[ip] = recent
    return len(recent)


def blocked(ip: str) -> bool:
    return failures_for(ip) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    now = time.time()
    _failures.setdefault(ip, []).append(now)
    if len(_failures) > MAX_TRACKED_IPS:
        # a spoofable client key must not be able to grow this dict forever
        for key in [k for k, v in _failures.items() if not v or now - v[-1] > FAILURE_WINDOW]:
            _failures.pop(key, None)
        if len(_failures) > MAX_TRACKED_IPS:
            for key in list(_failures)[: len(_failures) - MAX_TRACKED_IPS]:
                _failures.pop(key, None)


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)
