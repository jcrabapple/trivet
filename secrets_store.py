"""Encrypted storage for search-engine API keys entered through the web UI.

Keys are write-only from the interface's point of view: the page shows the last four
characters and nothing else. The ciphertext lives beside the query cache, encrypted
with AES-256-GCM under a key derived from the operator's session secret, so a copy of
the database without that secret is not a copy of the credentials.

If no secret is configured, storing refuses rather than falling back to plaintext.
"""
from __future__ import annotations

import base64
import os
import sqlite3
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DB_PATH = os.path.expanduser(os.environ.get("TRIVET_DB", "~/.cache/trivet/cache.db"))
SALT = b"trivet-api-keys-v1"


class NoSecret(RuntimeError):
    """Raised when no secret is configured, so nothing can be encrypted safely."""


def _secret() -> str:
    for name in ("TRIVET_SECRET_KEY", "TRIVET_SESSION_SECRET"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise NoSecret("Set TRIVET_SECRET_KEY (or TRIVET_SESSION_SECRET) to store API keys.")


def _aead() -> AESGCM:
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT, info=b"").derive(
        _secret().encode())
    return AESGCM(raw)


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            name       TEXT PRIMARY KEY,
            nonce      BLOB NOT NULL,
            ciphertext BLOB NOT NULL,
            last4      TEXT NOT NULL,
            updated_at REAL NOT NULL
        )""")
    return conn


def save(name: str, value: str) -> None:
    value = (value or "").strip()
    if len(value) < 8:
        raise ValueError("Key looks too short to be real.")
    nonce = os.urandom(12)
    ct = _aead().encrypt(nonce, value.encode(), name.encode())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO api_keys (name, nonce, ciphertext, last4, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "nonce=excluded.nonce, ciphertext=excluded.ciphertext, "
            "last4=excluded.last4, updated_at=excluded.updated_at",
            (name, nonce, ct, value[-4:], time.time()))


def delete(name: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM api_keys WHERE name = ?", (name,))


def load() -> dict[str, str]:
    """Decrypt every stored key. Unreadable rows are skipped, not fatal."""
    out: dict[str, str] = {}
    try:
        with _conn() as conn:
            rows = conn.execute("SELECT name, nonce, ciphertext FROM api_keys").fetchall()
    except sqlite3.Error:
        return out
    aead = _aead()
    for name, nonce, ct in rows:
        try:
            out[name] = aead.decrypt(nonce, ct, name.encode()).decode()
        except Exception:  # noqa: BLE001 - rotated secret makes old rows unreadable
            continue
    return out


def status() -> dict[str, dict]:
    """What the UI may show: last four characters and when it was set."""
    out: dict[str, dict] = {}
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT name, last4, updated_at FROM api_keys").fetchall()
    except sqlite3.Error:
        return out
    for name, last4, updated in rows:
        out[name] = {"last4": last4, "updated_at": updated}
    return out


def available() -> bool:
    """True when a secret exists, i.e. when the UI may accept keys."""
    try:
        _secret()
        return True
    except NoSecret:
        return False
