# Trivet

A search engine for humans, backed by agentic search APIs.

![Trivet search results](docs/screenshot-results.png)

SearXNG's shape, not SearXNG's engines. Instead of scraping seventy HTML front ends, each
query fans out to a few independent commercial indexes — Keenable, Exa, Brave, Tavily,
Google via Serper, Linkup, Firecrawl, Valyu — and the results are fused into one ranked
list you can actually read.

It is deliberately not a tool for AI agents: no function-calling schema, no raw JSON blob
for a model to chew on. It is a page with a search box, server-rendered, that works with
JavaScript off.

## Why bother

Those APIs each rank on their own scale. Exa returns a similarity float, Valyu a relevance
score, Keenable nothing at all, Serper a Google position. Averaging them is meaningless.
Trivet fuses on **ranks** with Reciprocal Rank Fusion:

```
score(url) = Σ_engines 1 / (60 + rank_in_engine)
```

A URL four engines agree on beats a single engine's rank-1 hit, because four independent
crawls agreeing is stronger evidence than any one vendor's confidence score.

Then a second stage re-ranks locally with signals no vendor exposes:

- **Intent** — "best browser" wants current options; "asyncio create_task" wants the docs,
  however old they are.
- **Staleness** — pages older than 18 months lose points, older than three years lose
  more. A year in the title or the URL slug counts as a claim about when it was written.
- **SEO listicles** — penalised by *shape × (1 − publisher trust)*, so "the 9 best Android
  browsers, tested" from a reputable tech site survives and the same listicle on an unknown
  host does not.
- **Page type over domain** — a GitHub repo is reference material, a GitHub *issue* is a
  forum post. Scoring them the same put an issue thread above the actual Chrome Web Store
  listings for "bookmark sync extension".
- **Domain reputation** — optional AdGuard Home filter lists plus a local seed list, shown
  as a badge rather than hidden: a blocklist hit is evidence about the host, not a verdict
  on the page.

## Quickstart

Python 3.10+.

```bash
git clone <this repo> trivet && cd trivet
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # edit it
python3 app.py --port 8952
```

Or with Docker:

```bash
docker build -t trivet .
docker run -p 8952:8952 -v trivet-data:/data \
  -e TRIVET_USER=admin -e TRIVET_PASS=change-me trivet
```

Open <http://127.0.0.1:8952>. With no keys configured the page still loads and tells you
that no engines are available.

## API keys

Every backend needs its own key. Two ways in; the web UI is the intended one.

**In the browser** — `/settings/keys`. Paste a key, press Save. It is encrypted with
AES-256-GCM under a key derived from `TRIVET_SECRET_KEY`, stored in the local SQLite
database, and afterwards shown only as `···abcd`. The engine appears in Preferences the
moment its key is saved, no restart.

Set a secret first, or storage refuses to accept keys rather than falling back to
plaintext:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**In the environment** — for containers and unattended installs set the variable directly.
Precedence: `.env` file → keys stored in the web UI → exported environment variables, so an
explicit export always wins.

| Engine | Variable | Tier | What it gives you |
|---|---|---|---|
| Keenable | `KEENABLE_API_KEY` | fast | agent-first index |
| Exa | `EXA_API_KEY` | fast | neural index with page text |
| Brave | `BRAVE_API_KEY` | fast | independent crawl (free plan is 1 req/s, paced automatically) |
| Tavily | `TAVILY_API_KEY` | fast | index tuned for RAG |
| Serper | `SERPER_API_KEY` | fast | real Google results |
| Linkup | `LINKUP_API_KEY` | fast | EU-hosted index |
| Firecrawl | `FIRECRAWL_API_KEY` | fast | crawl-and-extract index |
| Valyu | `VALYU_API_KEY` | deep | agentic retrieval, full page text, 5–6s cold |
| Parallel | `PARALLEL_API_KEY` | deep | objective-driven search |

Adding an engine is one adapter plus one `Spec` in `providers.py`, which declares the env
var that holds its key, its tier, and which query options it honours.

### Which set to run

Measured on 26 labelled queries with `eval/combo_bench.py`:

| Set | nDCG@10 | recall@20 | wall time |
|---|---|---|---|
| Exa + Brave + Serper | 0.735 | 92.3% | 1.3s |
| **Keenable + Exa + Brave + Serper** | 0.709 | **96.2%** | 1.3s |
| all seven fast engines | 0.732 | 96.2% | 2.1s |

The default is the four-engine set: highest recall inside a ~1.3s budget. Ranking quality
alone prefers three engines, but that loses the answer on one query in twenty-six. More is
not automatically better — weak engines add noise to the consensus signal and buy no recall.

Deep engines are opt-in twice: tick them in Preferences, and by default they only run when a
fast engine failed or the merged set could not fill a page.

## Security

A public instance must have credentials: the process refuses to start on a non-loopback
address without them, because every search spends the operator's API budget. Keys are
encrypted at rest and write-only in the UI. Every response carries a CSP, and only
`http(s)` URLs ever become links.

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md). The full pre-release
review and what was changed in response are in `docs/SECURITY-REVIEW.md`.

## Configuration

Environment variables, read at startup.

| Variable | Default | Purpose |
|---|---|---|
| `TRIVET_USER` / `TRIVET_PASS` | unset | HTTP Basic credentials. **Unset means no auth** — fine on localhost, not on the internet |
| `TRIVET_SECRET_KEY` | unset | encryption key for API keys stored in the UI |
| `TRIVET_SESSION_SECRET` | random per boot | signs session cookies; set it or logins do not survive a restart |
| `TRIVET_SESSION_DAYS` | 90 | session lifetime |
| `TRIVET_DB` | `~/.cache/trivet/cache.db` | cache, feedback log, reputation cache, stored keys |
| `TRIVET_CACHE_TTL` | 900 | seconds a query result stays cached |
| `TRIVET_ADGUARD_URL` | unset | AdGuard Home base URL for domain reputation |
| `TRIVET_ADGUARD_PASSWORD_FILE` | `~/.config/trivet/adguard-password` | AdGuard admin password |
| `TRIVET_INFISICAL_PROJECT` / `TRIVET_INFISICAL_TOKEN_FILE` | unset | optional: also read keys from an Infisical vault |

A public instance needs auth. Every query spends real money against at least one paid API,
so an open instance is somebody else's free search engine.

## Features

- **Server-rendered** — no build step, no framework in the browser, works with JS off.
- **Bookmarkable everything** — query, page, engines, time filter, safe search, results per
  page are all in the URL.
- **Preferences** as cookies: engines by tier, theme, safe search, results per page,
  open-in-new-tab, deep-engine policy.
- **Query cache** in SQLite keyed on query + engines + filters + fetch depth. A repeat is
  instant, and a ranking change still applies immediately because candidates are cached,
  not the ordering.
- **Per-engine timing** on every result page, plus `/stats` with p50/p95 over 24h and 7d.
- **Feedback loop** — impressions and clicks logged with the feature snapshot. Nothing is
  trained on it yet, deliberately.
- **Bangs** — `!w query` jumps straight to a site, using the Kagi bang dataset.
- **Small-web prior** — curated independent sites get a boost.
- **OpenSearch** descriptor, so the browser can add Trivet as a search engine.

## Deployment

Tested shape: one systemd user service on localhost behind a Cloudflare tunnel. Any reverse
proxy works the same way — the app only needs to believe it is behind TLS
(`X-Forwarded-Proto`) so it can mark cookies `Secure`.

```ini
# ~/.config/systemd/user/trivet.service
[Service]
WorkingDirectory=%h/projects/trivet
EnvironmentFile=%h/.config/trivet/trivet.env
ExecStart=/usr/bin/python3 %h/projects/trivet/app.py --host 127.0.0.1 --port 8952
Restart=on-failure
ReadWritePaths=%h/.cache/trivet
```

```bash
systemctl --user enable --now trivet.service
cloudflared tunnel create trivet
cloudflared tunnel route dns trivet trivet.example.com
```

## How it is built

| File | Role |
|---|---|
| `providers.py` | one adapter per vendor, key resolution, tiering, throttles |
| `merge.py` | URL canonicalisation and RRF fusion |
| `ranking.py` | intent detection, scoring, penalties, diversification |
| `quality.py` | listicle detection by shape and publisher trust |
| `reputation.py` | AdGuard lookups, local lists, host heuristics |
| `render.py` | all HTML and CSS, server-side |
| `app.py` | routes, auth, cache, request pipeline |
| `secrets_store.py` | encrypted API-key storage for the web UI |
| `cache.py`, `feedback.py`, `stats.py` | SQLite: cache, impressions/clicks, timing history |
| `eval/` | labelled queries, nDCG harness, engine-set benchmark |

`docs/NOTES.md` carries the longer engineering log: measured numbers, dead ends, and the
reasoning behind each weight.

## Evaluation

The numbers above come from 26 labelled queries (22 tuning, 4 holdout) scored with nDCG@10
and recall@20 against expected URLs.

```bash
python3 eval/eval_rank.py --save-snapshot eval/snapshot.json   # fetch once
python3 eval/eval_rank.py --snapshot eval/snapshot.json        # score offline
python3 eval/combo_bench.py --fetch                            # engine-set comparison
```

Two rules, learned the hard way: never compare weightings against a live refetch (engine
drift moves nDCG by 0.2 between identical runs), and never judge an engine with a harness
that bypasses its rate limit — that measures your own burst rate.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

The suite covers the parts where a regression is a security problem: the startup
credential guard, URL scheme filtering, `?next=` handling, key encryption and its
binding to a key name, cache-key completeness, session expiry and rate limiting. CI runs
it on Python 3.10, 3.12 and 3.13.

## Licence

MIT. See [LICENSE](LICENSE).
