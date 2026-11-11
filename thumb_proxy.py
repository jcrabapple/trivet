"""Signed, SSRF-hardened thumbnail proxy for image search.

Trivet ships `img-src 'self' data:` so search results never leak the user's IP or
timing to random third-party hosts. Image search routes thumbnails through `/img`
using an HMAC-signed URL minted during server-side rendering:

  1. Only URLs signed by this instance are fetched (no open proxy).
  2. Every DNS answer on every redirect hop must be a global unicast address
     (blocks loopback, RFC1918, link-local, CGNAT, and IPv4-mapped IPv6).
  3. Only raster image types are returned, capped at 4 MB, with SVG and HTML
     explicitly rejected so `/img` can never execute active content.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import socket
from collections import OrderedDict
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

import auth

MAX_URL_LEN = 2048
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3
SIG_HEX_LEN = 24

CACHE_MAX_ITEMS = 96
CACHE_MAX_BYTES = 12 * 1024 * 1024

ALLOWED_MEDIA_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
    "image/avif": "image/avif",
}


class ProxyError(Exception):
    """Upstream image could not be fetched or failed validation."""


class DisallowedTarget(ProxyError):
    """URL failed scheme, hostname, or public-IP validation."""


_CLIENT: httpx.AsyncClient | None = None
_SEM = asyncio.Semaphore(12)
_LRU: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_LRU_BYTES = 0


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=False,
            headers={"User-Agent": "trivet/0.2 (+image-proxy)"},
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )
    return _CLIENT


def _valid_http_url(url: str) -> bool:
    if not url or len(url) > MAX_URL_LEN:
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def sign_url(url: str) -> str:
    """Mint a compact HMAC tag binding `url` to this instance's session secret."""
    if not _valid_http_url(url):
        return ""
    secret = auth.session_secret()
    if not secret:
        return ""
    key = b"trivet-thumb-v1:" + secret.encode()
    return hmac.new(key, url.encode(), hashlib.sha256).hexdigest()[:SIG_HEX_LEN]


def verify_sig(url: str, sig: str) -> bool:
    if not sig or len(sig) != SIG_HEX_LEN:
        return False
    expected = sign_url(url)
    if not expected:
        return False
    return hmac.compare_digest(expected, sig)


def proxy_path(url: str) -> str:
    sig = sign_url(url)
    if not sig:
        return ""
    return "/img?" + urlencode({"u": url, "sig": sig})


def is_public_ip(raw: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        # Strip IPv6 zone identifiers if present.
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_global
        and not (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    )


async def _ensure_public_target(url: str) -> None:
    if not _valid_http_url(url):
        raise DisallowedTarget("invalid URL")
    parts = urlsplit(url)
    host = (parts.hostname or "").strip()
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise DisallowedTarget("invalid port") from exc

    # Literal IP fast-path without touching DNS.
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        if not is_public_ip(host):
            raise DisallowedTarget("non-public IP literal")
        return

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ProxyError("DNS lookup failed") from exc
    if not infos:
        raise ProxyError("no DNS records")
    for *_, sockaddr in infos:
        if not sockaddr or not is_public_ip(str(sockaddr[0])):
            raise DisallowedTarget("resolved to non-public address")


def detect_raster_type(content_type: str, head: bytes) -> str | None:
    """Return a safe raster MIME type, rejecting SVG, HTML, and unknown blobs."""
    if not head:
        return None
    stripped = head.lstrip()
    if stripped.startswith((b"<", b"<!--")):
        return None

    # Sniff magic bytes first so mislabelled CDNs (octet-stream) still work and
    # mislabelled HTML/SVG pretending to be image/png never slips through.
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (
        b"avif", b"avis", b"mif1"
    ):
        return "image/avif"

    base = (content_type or "").split(";", 1)[0].strip().lower()
    return ALLOWED_MEDIA_TYPES.get(base)


def _cache_get(url: str) -> tuple[bytes, str] | None:
    hit = _LRU.get(url)
    if hit is not None:
        _LRU.move_to_end(url)
    return hit


def _cache_put(url: str, data: bytes, media_type: str) -> None:
    global _LRU_BYTES
    if len(data) > CACHE_MAX_BYTES // 4:
        return
    old = _LRU.pop(url, None)
    if old is not None:
        _LRU_BYTES -= len(old[0])
    _LRU[url] = (data, media_type)
    _LRU_BYTES += len(data)
    while len(_LRU) > CACHE_MAX_ITEMS or _LRU_BYTES > CACHE_MAX_BYTES:
        _, (evicted, _) = _LRU.popitem(last=False)
        _LRU_BYTES -= len(evicted)


async def fetch_thumbnail(
    url: str, client: httpx.AsyncClient | None = None
) -> tuple[bytes, str]:
    """Fetch, validate, and size-cap a remote thumbnail."""
    cached = _cache_get(url)
    if cached is not None:
        return cached

    http = client or _client()
    cur = url
    async with _SEM:
        for _ in range(MAX_REDIRECTS + 1):
            await _ensure_public_target(cur)
            try:
                async with http.stream(
                    "GET",
                    cur,
                    headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.5"},
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location", "").strip()
                        if not loc:
                            raise ProxyError("redirect without location")
                        cur = urljoin(cur, loc)
                        continue
                    if resp.status_code != 200:
                        raise ProxyError(f"HTTP {resp.status_code}")

                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > MAX_IMAGE_BYTES:
                        raise ProxyError("image exceeds size cap")

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise ProxyError("image exceeds size cap")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    content_type = resp.headers.get("content-type", "")
            except httpx.HTTPError as exc:
                raise ProxyError(type(exc).__name__) from exc

            media_type = detect_raster_type(content_type, body[:32])
            if not media_type:
                raise ProxyError("unsupported media type")
            _cache_put(url, body, media_type)
            return body, media_type

    raise ProxyError("too many redirects")
