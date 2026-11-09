# Security review — trivet

Scope: static review of the working tree at `bd26f2c` (plus uncommitted changes to
`app.py`, `render.py`, `providers.py`, `reputation.py`). Read-only; no code was modified.
Files read in full: `app.py`, `auth.py`, `secrets_store.py`, `cache.py`, `feedback.py`,
`stats.py`, `reputation.py`, `bangs.py`, `merge.py`, `render.py`, `requirements.txt`,
`.env.example`. `providers.py` was read through line 230 (helpers and two adapters).

---

## 1. Verdict

Not today, not as shipped. The crypto and SQL layers are careful and the HTML escaping is
disciplined — this is not a codebase riddled with holes — but access control is fail-open
and unguarded, the login throttle is keyed on a header any client can set, and there is no
CSP or security header anywhere. Fix the four HIGH/MEDIUM items below (auth guard, throttle
keying, result-URL scheme check, headers) and it is reasonable to expose behind a proxy.

---

## 2. Findings

### HIGH — Authentication is opt-in, fails open, and silently disables itself on a partial config

`app.py:48-50`, `app.py:104`, `app.py:566-574`

```python
AUTH_USER = os.environ.get("TRIVET_USER", "")
AUTH_PASS = os.environ.get("TRIVET_PASS", "")
...
if not (AUTH_USER and AUTH_PASS) or path in PUBLIC_PATHS:
    return await call_next(request)
```

`and`, not `or`: setting `TRIVET_USER` and forgetting `TRIVET_PASS` leaves the entire app
open with no warning in the log. `.env.example:5-7` tells the operator to "leave both unset
for a localhost-only instance", and `--host 0.0.0.0` (`app.py:574`) is accepted without
comment — so the documented path from "works on my laptop" to "reachable through a
Cloudflare tunnel" has no gate on it.

What an anonymous visitor gets on such an instance:

- **Unlimited searches billed to the operator.** Every `GET /search` fans out to all
  configured engines. There is no rate limit (finding 7). This is the whole cost model of
  the product handed to strangers.
- **`/settings/keys` is fully writable.** `_csrf()` returns `""` when there is no session
  cookie (`app.py:123-128`), the form embeds `value=""`, and `keys_post` compares
  `form.get("csrf") != _csrf(request)` → `"" != ""` is false, so the check passes
  (`app.py:418`). An anonymous POST with `action=delete` and a valid key name deletes the
  operator's credentials one at a time (`app.py:428-432`). Key *values* cannot be read back
  (see Confirmed safe), but they can be destroyed.
- `/debug/rank`, `/stats`, `/api/search`, and the full query history in `/stats`.

Fix — refuse to start rather than fail open:

```python
def _require_credentials():
    host = os.environ.get("TRIVET_BIND_HOST", "")
    if bool(AUTH_USER) != bool(AUTH_PASS):
        raise SystemExit("Set both TRIVET_USER and TRIVET_PASS, or neither.")
    if not (AUTH_USER and AUTH_PASS) and host not in ("", "127.0.0.1", "::1", "localhost"):
        raise SystemExit(
            f"Refusing to bind {host} with no credentials. "
            "Set TRIVET_USER and TRIVET_PASS.")
```

Call it in `__main__` before `uvicorn.run`, and pass `args.host` in. Also log a single
`WARNING: authentication disabled` line at startup whenever the pair is empty, so it shows
up in `journalctl` on a deployment that was never meant to be public.

Separately: `/healthz` is public and returns engine names and cache volume
(`app.py:504-507`). Harmless, but it confirms which paid engines a host is running.

### HIGH — Login throttle is keyed on a client-supplied header

`app.py:278-279`, `auth.py:80-95`

```python
ip = (request.headers.get("cf-connecting-ip")
      or (request.client.host if request.client else "unknown"))
```

Two failure modes, both real:

1. **Bypass.** Behind nginx (or any proxy that does not strip and rewrite
   `CF-Connecting-IP`) an attacker sends a fresh random value per request. Every request
   lands in a new bucket, `auth.blocked()` is never true, and the 10-per-15-minute limit
   does nothing. Password guessing becomes unthrottled against a single shared password.
2. **Lockout.** Behind a proxy that does *not* set the header, `request.client.host` is the
   proxy's address, so every visitor shares one bucket. Ten wrong passwords from anywhere
   lock the operator out for 15 minutes — and the attacker is not slowed at all.

Fix — only trust the header when the request actually came from a trusted peer, and
otherwise use the socket address:

```python
TRUSTED_PROXIES = {"127.0.0.1", "::1", "10.0.0.0/8"}  # configure, do not guess

def _client_ip(request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in TRUSTED_PROXIES:                      # or ipaddress.ip_network check
        fwd = request.headers.get("cf-connecting-ip") or \
              request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if fwd:
            return fwd
    return peer
```

Prefer `uvicorn --proxy-headers --forwarded-allow-ips=<proxy ip>`, which makes
`request.client.host` correct and removes the header handling entirely.

### MEDIUM — Result URLs are never scheme-checked before becoming `href`

`render.py:358`, `render.py:589`, `providers.py:175-203`

```python
<h2><a href="{escape(hit.url)}" ...>
```

`html.escape` neutralises `<`, `>`, `&`, quotes — it does **not** neutralise a `javascript:`
or `data:` scheme. `_hits()` accepts any non-empty `url` from the vendor payload with no
validation (`providers.py:182-184`), and `merge.MergedHit.url` carries it to the anchor
unchanged. Every other sink in the app that takes a URL does check: `bangs.resolve` requires
`http://`/`https://` (`bangs.py:99`), `/api/click` requires `startswith("http")`
(`app.py:499`). The two rendering sinks are the exception.

Attack: get a result with `javascript:` into any index that echoes URLs without normalising
them (agentic extractors such as Firecrawl/Valyu return URLs found in page content), wait
for the operator to search a matching query, and have them click it. The script runs in
trivet's origin. `HttpOnly` keeps the session cookie out of reach, so the impact is not
cookie theft — it is acting as the operator inside the app: reading every result via
`/api/search`, rewriting or deleting API keys via `POST /settings/keys` (the CSRF token is
readable from the DOM, so it is no obstacle), and burning spend.

Exploitability depends on a vendor passing a non-http scheme through, which none of the
adapters I read does explicitly. I rate it MEDIUM rather than HIGH for that reason, but the
fix is three lines and removes the class of bug entirely:

```python
from urllib.parse import urlsplit

def _safe_href(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "#"
    if parts.scheme.lower() not in ("http", "https"):
        return "#"
    return url
```

Apply in `_result` (`render.py:358`) and `rank_debug` (`render.py:589`). Better still, drop
non-http(s) hits in `providers._hits` so they never enter the cache or the impression log.

### MEDIUM — No security headers, no CSP

`app.py:52`, `render.py:227-260`

`FastAPI(title="trivet", docs_url="/api/docs")` installs no middleware. Responses carry no
`Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options` /
`frame-ancestors`, or `Referrer-Policy`. Three consequences:

- Any XSS that does slip through — the `javascript:` case above, or a future edit that
  misses an `escape()` — is fully weaponisable instead of crippled.
- The app can be framed by any site (`clickjacking` on `/settings/keys`, where a transparent
  iframe over the "Remove" button is a one-click key deletion).
- Every outbound result click sends a `Referer` naming the operator's trivet instance and
  the full query string (queries are in the URL) to the third-party site.

The page is a single inline `<style>` and one inline `<script>` (`render.py:236-260`), so a
strict CSP is cheap — there are no external assets:

```python
@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp
```

(`'unsafe-inline'` for script is required by the beacon; moving it to a static file with a
nonce is the better follow-up. Even with `unsafe-inline`, `base-uri`, `form-action` and
`frame-ancestors` still bite.) Register this *after* `require_auth` so it wraps it.

### MEDIUM — Session tokens are 90-day bearer cookies with no revocation path

`auth.py:24`, `auth.py:44-72`, `secrets_store.py:29-40`

The token is `base64(user|exp) . HMAC-SHA256(secret, payload)` — correct construction,
constant-time compare, expiry checked. But:

- `DEFAULT_DAYS = 90`. Once issued, a cookie cannot be invalidated: `/logout` only deletes
  the browser's copy (`app.py:298-303`). A cookie copied off a shared machine or out of a
  proxy log stays valid for a quarter.
- The only way to revoke every session is to change `TRIVET_SESSION_SECRET` — which is also
  the fallback encryption key for stored API keys (`secrets_store.py:29-34`). Rotating it
  silently makes every stored key undecryptable; `load()` swallows the exception and skips
  the row (`secrets_store.py:88-93`), so the engines just stop working with no error.
  Rotating the session secret to evict a stolen cookie therefore destroys the operator's
  credentials.
- `verify_token` returns whatever user string is in the payload and the middleware only
  checks truthiness (`app.py:106`), so a cookie issued for user `admin` still authenticates
  after `TRIVET_USER` is changed to something else.

Fix: put an issue timestamp in the payload and a `TRIVET_SESSION_EPOCH` env var; reject
tokens older than the epoch. That gives revocation without touching the encryption key:

```python
payload = f"{user}|{exp}|{int(time.time())}".encode()
...
user, exp, iat = payload.decode().rsplit("|", 2)
if int(iat) < int(os.environ.get("TRIVET_SESSION_EPOCH", "0")):
    return None
if user != AUTH_USER:          # needs AUTH_USER in scope; pass it in or import
    return None
```

Also lower `DEFAULT_DAYS` to 7–14, and use a distinct key for key-encryption: derive from
`TRIVET_SECRET_KEY` only, and refuse to fall back to the session secret
(`secrets_store.py:30`). Rotating a session secret should never destroy stored keys.

### MEDIUM — `q` has no length bound and is persisted per result row

`app.py:316`, `feedback.py:77-101`, `app.py:511`

`q: str = Query("", ...)` with no `max_length`. Consequences, all reachable by an
unauthenticated visitor on a misconfigured instance:

- The raw string is forwarded to up to eight vendor APIs, several of which will 414/400 and
  show as engine errors on the page.
- `feedback.log_impression` writes the query once **per result position** — a 100 KB query
  with 30 results writes ~3 MB into `impressions` per request.
- `cache.make_key` hashes it (fine), but `cache.put` stores `normalize_query(query)` in
  plaintext.

Fix: `q: str = Query("", max_length=512)` on both `/search` and `/api/search`, and truncate
in `search()` as a belt-and-braces (`query = query[:512]`). 512 is generous for a search
box; Google's own limit is well under that.

### MEDIUM — No rate limiting on the search paths

`app.py:313`, `app.py:510`

Only `/login` is throttled. `/search` and `/api/search` are not, and each one costs real
money per call. With auth enabled the exposure is "one leaked cookie = unlimited spend";
with auth disabled it is "the internet = unlimited spend". The cache absorbs repeated
identical queries for 15 minutes but nothing absorbs a loop over distinct queries.

Fix: a token bucket per session (or per IP when anonymous) in the same in-process style as
`auth._failures`:

```python
from collections import deque
_recent: dict[str, deque] = {}
LIMIT, WINDOW = 30, 60          # 30 searches / minute

def allow(bucket: str) -> bool:
    now = time.time()
    q = _recent.setdefault(bucket, deque())
    while q and now - q[0] > WINDOW:
        q.popleft()
    if len(q) >= LIMIT:
        return False
    q.append(now)
    return True
```

Bucket on the session token (or client IP), return `429` with `Retry-After` from
`/api/search` and a rendered error from `/search`. Prune `_recent` on write, and cap its
size, or it becomes the same unbounded-growth problem as finding 8.

### MEDIUM — Unbounded in-memory and on-disk growth

`auth.py:80`, `feedback.py:42-69`, `feedback.py:104-113`

- `_failures: dict[str, list[float]]` never drops keys. Combined with the spoofable
  `CF-Connecting-IP` above, each unique header value permanently allocates an entry (the
  list is emptied by `failures_for` but the dict key stays). A few million spoofed requests
  is a few hundred MB.
- `impressions` and `clicks` have **no retention at all**. `stats.py` prunes
  `engine_timings` at 45 days (`stats.py:15`); `feedback.py` prunes nothing. Every search
  adds `per` rows (10–30) and never removes them. A public instance reaches millions of rows
  and hundreds of MB in weeks. `/api/click` accepts an attacker-chosen `impression_id` and
  inserts a row per call, so it can also be filled directly.

Fix: prune in `log_impression` the same way `cache.put` does:

```python
conn.execute("DELETE FROM impressions WHERE ts < ?", (now - 90 * 86400,))
conn.execute("DELETE FROM clicks WHERE ts < ?", (now - 90 * 86400,))
```

and bound `_failures` — drop entries older than `FAILURE_WINDOW` on each `record_failure`,
or use a `collections.OrderedDict` capped at a few thousand keys.

### LOW — `?next=` is guarded by a denylist, not an allowlist

`app.py:258-262`

```python
if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
    return raw
```

I could not construct a working bypass: `//evil.com` is rejected, `/\evil.com` is rejected
by the backslash check, and control-character tricks (`/\t/evil.com`, `/%09/evil.com`) are
percent-encoded again by Starlette's `RedirectResponse` quoting, so the browser sees a path
on the same origin rather than a protocol-relative URL. Note also that `quote()` in the
middleware (`app.py:112`) encodes `?` and `&`, so `next` never carries a query string at
all — it round-trips to a 404 path. That is a functional bug, and it happens to shrink the
attack surface.

So: no exploit found, but the check is one character away from being wrong. Make it an
allowlist:

```python
from urllib.parse import urlsplit

def _safe_next(raw: str) -> str:
    if not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw):
        return "/"
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return "/"
    return raw
```

### LOW — Host header flows unescaped into the OpenSearch document

`app.py:457-461`, `render.py:827-833`

```python
base = str(request.base_url).rstrip("/")
return Response(render.OPENSEARCH.format(base=base), media_type="...")
```

`request.base_url` is built from the `Host` header. `.format()` here is plain string
formatting — no escaping — so a request with `Host: x"><Url template="https://evil/..."` is
reflected into the XML. The response is served as
`application/opensearchdescription+xml`, so this is not browser-rendered HTML XSS; the
impact is a poisoned search-provider description for any client that fetches the document
(a browser adding trivet as a search engine would then send queries to the attacker's
template). Theoretical for the documented single-operator deployment, and only reachable if
the proxy forwards an arbitrary Host.

Fix: escape and validate.

```python
from xml.sax.saxutils import escape as xml_escape
from urllib.parse import urlsplit

parts = urlsplit(str(request.base_url))
if parts.hostname not in ALLOWED_HOSTS:      # from env, default to the request's own
    return Response(status_code=400)
base = xml_escape(f"{parts.scheme}://{parts.netloc}")
```

Or simply add `TrustedHostMiddleware` with the deployment's hostname.

### LOW — `f.intent` is interpolated unescaped (uncertain)

`render.py:590`

```python
f"<div class='d'>{escape(item.hit.domain)} · {f.intent}"
```

`domain` is escaped on the same line; `intent` is not. `Features.intent` defaults to
`"general"` and every use I saw compares it against literals (`"recency"`, `"tech"`,
`startswith("tech")`), which strongly suggests `detect_intent()` returns from a closed set —
but **I did not read `detect_intent`'s body** (`ranking.py:252` onward), so I cannot assert
it never echoes query text. If it does, this is reflected XSS on `/debug/rank`. Marking it
as unverified rather than claiming it is safe.

Fix, one token: `{escape(f.intent)}`. Same for the `weights` join at `render.py:596`
(config-file values, not user input, but escaping costs nothing).

### LOW — State-changing GETs: `/logout` and `/preferences?save=1`

`app.py:298-303`, `app.py:357-390`

Both are `GET` and both mutate state (cookie deletion; six preference cookies). `/logout` is
in `PUBLIC_PATHS`, so any page can log the operator out with an `<img src>`. `/preferences?
save=1` lets a cross-site navigation rewrite the victim's engine selection, safesearch level
and theme. Impact is confined to the victim's own browser and is reversible; the CSRF
consequence is low. Worth knowing, not worth an incident.

Fix: make `/logout` a `POST` with the CSRF token (or accept both and only act on POST), and
make `/preferences` `POST` for the save path while keeping `GET` for rendering the form.

### LOW — Cache key omits the deep-engine policy

`app.py:187`, `app.py:340`

`cache.make_key(query, engines, time_range, safesearch, fetch)` keys on the *requested*
engine list, but whether the deep engines actually ran depends on `deep_policy` and on
whether the fast tier came up short (`app.py:211-221`). A search that skipped Valyu stores
results under the same key as one that ran it. With `deep=always` selected, the operator can
be served a cached page that silently lacks the deep engine's results for the rest of the
TTL. Not a security bug — a correctness one — but it is the kind of cache-key gap the brief
asked about, and it is the only one I found: `ss`, `time_range`, the engine list and the
fetch depth are all correctly in the key, and `ranker` is deliberately excluded because the
cache stores the unranked candidate set (`app.py:231-233`).

Fix: add `deep_policy` to `make_key`, or record which engines actually answered and refuse a
cache hit when the requested policy is `always` and a deep engine is absent from the cached
`engines` list.

### LOW — Dependencies are unpinned

`requirements.txt:1-5`

`fastapi>=0.115`, `uvicorn>=0.30`, `httpx>=0.27`, `cryptography>=42`,
`python-multipart>=0.0.9`. No upper bounds, no lock file, no hashes. A fresh install a year
from now resolves to whatever is current, and a compromised or breaking release lands in
every deployment. `python-multipart` in particular has a CVE history.

Fix: pin exact versions and add a `requirements.lock` generated with `pip-compile --generate-hashes`,
plus a Dependabot/Renovate config. For a project that stores paid credentials, reproducible
builds are worth the friction.

### LOW — Key ciphertext sits in a world-readable SQLite file alongside plaintext query history

`cache.py:26-39`, `feedback.py:37-41`, `secrets_store.py:43-56`, `providers.py:58-88`

`~/.cache/trivet/cache.db` is created by `sqlite3.connect` with the process umask — 0644
under the usual `022`, so any local user can read it. It contains AES-GCM ciphertext plus
`last4` of every API key (fine, by design), and also every query the operator has ever typed
and every URL shown, in plaintext. The Infisical cache gets `os.chmod(..., 0o600)`
(`providers.py:88`) but the main database does not.

Fix: `os.chmod(DB_PATH, 0o600)` after connect (and the `-wal`/`-shm` siblings), or ship a
systemd unit with `UMask=0077`. Optionally store `hash(normalize_query(query))` instead of
the query text in `queries.query`, since nothing reads it back for display.

### LOW — Bangs are an open redirector by design, fed by an unverified download

`bangs.py:16-37`, `bangs.py:76-103`, `app.py:334-336`

`GET /search?q=!anything` issues a `302` to a URL taken from the downloaded table. That is
the feature. Two notes:

- It makes trivet usable as a redirect laundromat: `https://trivet.example/search?q=!w+<phish>`
  is a link on a domain the operator controls that lands anywhere. If the instance is public,
  expect it to be abused for phishing links. Decide deliberately; if not, gate the bang
  redirect behind auth or append a confirmation page.
- `refresh()` fetches `https://raw.githubusercontent.com/kagisearch/bangs/main/data/bangs.json`
  and installs it with no signature or hash check (`bangs.py:27-29`). A compromised upstream
  or a DNS-poisoned fetch at startup puts attacker-chosen templates in the table — and a
  template that passes the `http(s)://` check can still point at any host. Theoretical, but
  the blast radius is "every bang redirect goes somewhere else".

Fix: pin the upstream to a commit SHA in `URL`, and verify a SHA-256 of the body against a
constant before `os.replace`.

### LOW — AdGuard lookups let a search result trigger a DNS resolution inside the operator's network

`reputation.py:180-199`

`_agh_lookup` passes the result's hostname as a query parameter to a fixed, operator-configured
URL — it never steers the *request* anywhere, so this is not classic SSRF. But AdGuard's
`check_host` resolves the name, so a hostile result domain (`internal-host.corp`,
`metadata.google.internal`) makes the operator's resolver attempt a lookup for an
attacker-chosen name. That is a blind, DNS-only probe: no response body comes back, no port
is touched. Theoretical and low impact; noting it because the brief asked about anything
user-influenced reaching an internal address.

Fix if it bothers you: skip lookup for hosts that resolve to private ranges, or refuse
non-public TLDs (`.corp`, `.internal`, `.local`) before calling out.

---

## 3. Confirmed safe

These held up under specific inspection, and are worth recording because several of them
look suspicious at a glance.

**HTML escaping in `render.py` is complete and consistent.** Every value that originates
outside the process is escaped: `hit.title`, `hit.url`, `hit.snippet`, `hit.domain`,
`hit.published` (`render.py:343`, `355-362`), engine labels and error strings
(`render.py:348`, `380`, `502`), the reputation flag's `category` and `title`
(`render.py:336-338`), `q` in the search box, the `<title>`, and the empty-result message
(`render.py:265`, `232`, `409`). `_fmt_date` returns unparsed vendor dates verbatim
(`render.py:224`) but the single caller escapes the result. `data-theme` is both
allowlisted against `("light","dark")` and escaped (`render.py:228`).

**The inline `<script>` interpolates nothing.** The click beacon (`render.py:238-260`) reads
its data from `document.body.dataset.imp`, which is written by the server as an escaped
attribute (`render.py:237`), and the impression id itself is `secrets.token_urlsafe(9)`
(`feedback.py:73-74`). No server value is templated into JavaScript, so there is no JS
injection surface there even before considering CSP.

**No SQL injection.** Every statement in `cache.py`, `feedback.py`, `stats.py`,
`reputation.py` and `secrets_store.py` uses `?` placeholders, including the `LIMIT ?` in
`cache.put` (`cache.py:85-86`) and the `hours`-derived `since` in `stats.summary`
(`stats.py:67-73`). I found no f-string or `%` formatting into SQL anywhere.

**API keys cannot be read back through any interface I examined.** `keys_page` renders only
`status()[name]["last4"]` (`render.py:731`); `secrets_store.status()` selects `name, last4,
updated_at` and nothing else (`secrets_store.py:96-107`); `/healthz` returns engine names and
cache counts (`app.py:504-507`); `/api/search` returns per-engine `error` strings that come
from `httpx` exceptions, which carry the URL, not the headers. The key value is also bound
as GCM associated data (`secrets_store.py:64`, `90`), so a ciphertext row cannot be swapped
between key names. `/settings/keys` POST validates the key name against the set of names
declared in `SPECS` (`app.py:422-425`), so an attacker cannot create an arbitrary row name
that later gets loaded into the environment.

**The session HMAC construction is sound.** `hmac.new(secret, payload, sha256)` over the
exact bytes that are base64-encoded, `hmac.compare_digest` for verification, expiry parsed
from the signed payload rather than the cookie's `max_age` (`auth.py:40-72`). An attacker
without `TRIVET_SESSION_SECRET` cannot forge a token, and a tampered expiry fails the
signature check. Basic auth also uses `compare_digest` on both fields (`app.py:93`).

**Cookie flags are mostly right.** `httponly=True`, `samesite="lax"`, `path="/"`
(`app.py:292-294`). `Secure` is conditional on `X-Forwarded-Proto` — see "Could not verify".

**No SSRF through `providers.py`.** Every adapter URL I read is a hardcoded vendor endpoint
(`https://api.keenable.ai/v1/search`, `https://api.exa.ai/search`); the query is placed in
the request *body*, never in the URL or headers, and the vendor set is a closed list from
`SPECS`. Nothing user-supplied can change the destination. `reputation.py`'s URL comes only
from `TRIVET_ADGUARD_URL` (env), not from a request.

**Cache isolation between users is correct.** The key covers normalised query, sorted engine
list, `time_range`, `safesearch` and fetch depth (`cache.py:47-50`), so two visitors with
different safesearch or engine preferences cannot read each other's cached payload. The
cached row stores the *unranked* candidate set and ranking re-runs per request
(`app.py:231-233`, `195`), so a cached page still respects each visitor's ranker choice.
Cache growth is bounded to 1000 rows and 7 days (`cache.py:23`, `83-86`).

**CSRF works when it matters.** With auth enabled, the token is `HMAC(session_token,
"trivet-csrf")` and the session token is not guessable, so a cross-site POST cannot produce
it. It is compared with `!=` rather than `compare_digest` (`app.py:418`) — a timing side
channel on a 24-hex-char token that an attacker cannot usefully exploit without already
holding the session. It is a no-op when auth is disabled, which is covered under finding 1
rather than repeated here.

**`/api/*` does not leak.** `require_auth` runs on every path and returns `401` JSON for
anything under `/api/` (`app.py:110-111`), so `/api/search`, `/api/click` and `/api/docs`
are all behind the same gate as the HTML. There is no route registered before the middleware
that could bypass it, and `/api/docs` does not expose anything the operator cannot already
see.

---

## 4. Could not verify

- **`Secure` on the session cookie behind a real proxy.** `secure=(proto == "https")` reads
  `X-Forwarded-Proto` (`app.py:291-293`). Whether the flag is actually set depends on the
  Cloudflare tunnel or nginx config, which is not in the repo. If the header is absent the
  cookie ships without `Secure` over a plain-HTTP hop to the proxy. Needs a live deployment
  to confirm; the fix (set it unconditionally when a proxy terminates TLS, or force it via a
  `TRIVET_HTTPS=1` env flag) is independent of the verification.
- **Vendor error strings.** I read `_hits` and two adapters, not `run_engine` or the
  remaining six adapters (`providers.py:230-427`). My "no key in errors" conclusion rests on
  `httpx.raise_for_status()` formatting the URL rather than headers, and on no code I read
  interpolating a key into a message. A `logging` call in the unread half could still write
  one; grep for `logger`/`print` in that file before publishing.
- **`detect_intent`'s return values** (`ranking.py:252`) — see the `f.intent` finding. One
  function body, unread.
- **`ranking.py`, `quality.py`, `smallweb.py`, `refresh_data.py`, `eval_rank.py`,
  `analyze_feedback.py`** were not reviewed. `ranking.py` is the largest unread file (23 KB)
  and does string work on query text; `smallweb.py` also downloads a remote dataset at
  startup. Neither renders HTML directly, so neither is on the XSS path, but neither is
  cleared.
- **Actual browser behaviour of the `?next=` control-character cases.** My conclusion that
  `/\t/evil.com` and `/%09/evil.com` are not exploitable rests on Starlette's
  `RedirectResponse` percent-encoding the URL. That needs a browser and a running instance to
  confirm; the allowlist fix removes the question.
- **`/debug/rank` cost.** It issues two full searches per request (`app.py:478-481`), so it
  doubles vendor spend and doubles the cache write. Auth-gated, so it is an operator footgun
  rather than an attack — but if it is ever made public, it is a free amplifier for exactly
  the DoS in finding 7.
- **Whether a vendor will actually pass a `javascript:` URL through.** The scheme-check
  finding is real in the code; whether it is reachable depends on vendor-side normalisation
  I cannot test without keys and live traffic.

---

## 5. Triage — what was changed in response

Reviewed 2026-11-09 against `bd26f2c`. Item numbers follow the findings above.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | HIGH | Auth fails open, keys writable when unauthenticated | **Fixed.** `_check_auth_config()` refuses to start on a half-configured pair, and refuses a non-loopback bind with no credentials. A loopback instance without credentials logs a warning and blocks `POST /settings/keys`. |
| 2 | HIGH | Login throttle keyed on a spoofable header | **Fixed.** `_client_ip()` believes `CF-Connecting-IP`/`X-Forwarded-For` only when the peer is loopback. |
| 3 | MEDIUM | No scheme check before `href` | **Fixed twice over.** `render._safe_href()` allows only http/https, and `providers._hits()` drops other schemes at ingest so they never reach the cache or the impression log. |
| 4 | MEDIUM | No security headers | **Fixed.** CSP (`default-src 'none'`, `frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'self'`), `nosniff`, `no-referrer`, `X-Frame-Options: DENY`, applied to every response including redirects. |
| 5 | MEDIUM | 90-day bearer sessions, no revocation, shared secret | **Fixed.** Payload now carries issue time; `TRIVET_SESSION_EPOCH` evicts sessions without touching a secret. Default lifetime 14 days. `TRIVET_SECRET_KEY` is now the key-encryption secret and existing rows were re-encrypted with `secrets_store.migrate()`, so rotating the session secret no longer destroys stored keys. |
| 6 | MEDIUM | Unbounded `q` | **Fixed.** `max_length=512` on both entry points plus a truncation inside `search()`. |
| 7 | MEDIUM | No rate limit on search | **Fixed.** 40 requests/minute per session (or client IP when anonymous); 429 with `Retry-After`, JSON for `/api/*`, a rendered page for HTML. |
| 8 | MEDIUM | Unbounded growth | **Fixed.** Impressions and clicks prune at 90 days; the failure table is capped at 4096 keys; the rate-limit map prunes itself. |
| 9 | LOW | `?next=` denylist | **Fixed** as an allowlist that rejects schemes, netloc and control characters. |
| 10 | LOW | Host header reflected into OpenSearch XML | **Fixed.** Host is allowlisted against `TRIVET_HOSTS`, validated character-wise, and XML-escaped; `TRIVET_BASE_URL` overrides it entirely. |
| 11 | LOW | `f.intent` unescaped | **Fixed.** Escaped. (`detect_intent` returns from a closed set, but escaping costs one call.) |
| 12 | LOW | State-changing GETs (`/logout`, `/preferences?save=1`) | **Accepted.** Impact is confined to the victim's own browser and is reversible: a forged logout, or rewritten display preferences. Worth a POST in a later revision; not worth breaking every existing bookmark for. |
| 13 | LOW | Cache key omits deep-engine policy | **Fixed.** `deep_policy` is part of the key. |
| 14 | LOW | Unpinned dependencies | **Fixed.** Exact pins verified by installing into a fresh venv. |
| 15 | LOW | World-readable database | **Fixed.** `cache.db`, `-wal` and `-shm` are chmod 0600 on connect in all four modules that open it. |
| 16 | LOW | Bangs are an open redirector fed by an unsigned download | **Accepted, documented.** The redirect is the feature. An operator who exposes the instance publicly should expect `?q=!x` links to be used as a laundromat; pinning the upstream to a commit SHA is a reasonable follow-up for a release build. |
| 17 | LOW | AdGuard lookup makes the operator's resolver probe an attacker-chosen name | **Accepted.** Blind DNS-only probe, no response body, no port touched. |

Nothing was dismissed as a false positive; the two accepted items are product decisions
rather than disagreements with the finding.
