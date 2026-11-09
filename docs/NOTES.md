# Trivet

A search engine for humans, backed by agentic search APIs. SearXNG's shape, not
SearXNG's engines: instead of scraping 70 HTML sites, each query fans out to a few
independent commercial indexes and the results are fused into one list.

Live at the operator's own hostname, served from `127.0.0.1:8952` behind a Cloudflare
tunnel. Nothing in the code depends on that hostname.

Brand assets (icon, wordmark, lockup, favicon) live in `docs/brand/`; see
`docs/brand/README.md` for the construction rules, clearspace and colours.

## Engines

| Engine | Default | Tier | Notes |
|---|---|---|---|
| Keenable | on | fast | agent-first index, no scores returned |
| Exa | on | fast | neural index, returns page text for snippets |
| Brave | on | fast | independent crawl; time + safe search. Free plan is 1 req/s, so the adapter spaces calls by 1.1s (otherwise a page flip returns HTTP 429 and silently drops the engine) |
| Tavily | on | fast | independent index tuned for RAG; free tier is 1,000 credits/month, one credit per search |
| Valyu | off | deep | full page content, reports `total_deduction_dollars`; 2-6s |
| Serper | on | fast | real Google SERP. Measured 0.99-1.65s; contributes almost no unique URLs (0/0/1 over three probes) but overlaps 5-6 hits with the others, so its value is a trustworthy mainstream consensus signal rather than new coverage |
| Firecrawl | off | fast | crawl-and-extract index, 225ms-3.4s depending on whether their side has the query cached |
| Linkup | off | fast | EU-hosted, 0.9-1.5s, $5/1k |

Also implemented, waiting on a key: Parallel. Add the key and the engine appears in
preferences with no code change. Kagi was removed entirely when its key stopped working.

Engines that cannot be added: Bing (Microsoft retired the Search APIs on 2025-08-11) and
Google Custom Search JSON API (closed to new customers, discontinued 2027-01-01). Google
coverage therefore means a SERP reseller — Serper at $0.30-1.00 per 1,000 with 2,500 free
queries is the cheapest that keeps working.

Keys resolve from `.env` or `~/.config/trivet/trivet.env`, then keys stored through
`/settings/keys` (encrypted), then Infisical if configured, then the process environment.
An explicit export wins over everything.

## Why RRF

The engines do not share a scoring scale: Exa returns a similarity float, Valyu a
`relevance_score`, Keenable nothing at all, Brave its own internal rank. Averaging
those numbers is meaningless. Reciprocal Rank Fusion works on ranks only:

    score(url) = Σ_engines 1 / (60 + rank_in_engine)

A URL returned by three engines beats a rank-1 hit from one, which is exactly the
signal that a second-stage ranker exists to exploit. Exact-URL dedupe runs before ranking
(scheme, `www`, fragments and tracking parameters stripped). Syndicated-copy
collapsing exists but is **off**: it can pair one site's title with another site's
URL, and that reads to a user as a broken result.

## Routes

| Route | Purpose |
|---|---|
| `/` | home |
| `/search?q=&page=&time=&ss=&per=&engines=` | results, server-rendered, bookmarkable |
| `/preferences` | engines grouped by tier, theme, safesearch, results per page, open-in-new-tab, deep-engine policy (cookies) |
| `/about` | how the ranking works |
| `/opensearch.xml` | add to a browser search bar |
| `/api/search` | same search as JSON |
| `/healthz` | which engines are configured |

`time` is `any|day|week|month|year` and is applied where the engine supports it
(Exa `startPublishedDate`, Brave `freshness`, Tavily `days`, Linkup `fromDate`,
Serper/Firecrawl `tbs`, Kagi `t`). `ss` is `off|moderate|strict`, honored by Brave
and Serper. A filter an engine cannot honor is simply not sent; nothing pretends.

## Run

```bash
cd ~/projects/trivet
env -u PYTHONPATH python3 app.py --port 8952        # server
env -u PYTHONPATH python3 app.py --cli "query"      # terminal
```

`env -u PYTHONPATH` matters: Hermes exports its own PYTHONPATH and project venvs
break on it.

## Signing in

A password form issues a signed session cookie, so the browser stops re-prompting:

    ms_session = base64(user|expiry) . hmac_sha256(TRIVET_SESSION_SECRET, that)

- 90-day lifetime (`TRIVET_SESSION_DAYS`), `HttpOnly`, `SameSite=Lax`, and
  `Secure` whenever the request arrived over HTTPS.
- The signing secret lives in the unit's EnvironmentFile, so a service restart does
  not log you out, and the cookie cannot be forged without reading that file.
- `/login` accepts the password and returns you to the query you were trying.
- `/logout` clears the cookie.
- HTTP Basic still works for curl and scripts; API paths answer 401 JSON instead of
  redirecting, so a click beacon fails cleanly rather than parsing a login page.
- Ten failed attempts from one address in 15 minutes gets a 429 for the rest of the
  window, keyed on `CF-Connecting-IP` when it is present.
- `?next=` is restricted to same-site paths, so a crafted link cannot bounce a fresh
  login off-site.

## Deployment

Public behind HTTP Basic auth on the operator's hostname. The tunnel carries one ingress
rule per hostname, so an old hostname can remain alongside a new one during a rename.

| Piece | Value |
|---|---|
| App unit | `trivet.service` (systemd user, `127.0.0.1:8952`, enabled, linger on) |
| Tunnel unit | `cloudflared-trivet.service` |
| Tunnel | still named `metasearch` (names cannot be changed in place), id `f7a4c74f-7f0a-4655-a8ac-b25920d5d7c2` |
| Tunnel config | `~/.cloudflared/trivet.yml` |
| Credentials | `~/.config/trivet/trivet.env` (mode 600: `TRIVET_USER`, `TRIVET_PASS`, `TRIVET_SESSION_SECRET`, optional `TRIVET_SESSION_DAYS`) |
| Health (unauthenticated) | `GET /healthz` |
| Logs | `journalctl --user -u trivet.service -f` |

Auth is mandatory in practice: every query spends real money against Keenable, Exa
and Brave, and the tunnel is public. The middleware reads `TRIVET_USER` and
`TRIVET_PASS` from the environment; unset means open, which is only safe on
localhost. To rotate the password, edit the env file and
`systemctl --user restart trivet.service`.

The unit runs `/usr/bin/python3` with `ProtectHome=read-only` and
`ReadWritePaths=%h/.cache/trivet`, because the Infisical key cache lives there.


## Mobile

Below 640px the layout changes shape rather than shrinking:

- Header becomes two rows (brand + Preferences/About, then a full-width search field).
  The desktop single row measured 637px at a 390px viewport and pushed the nav links off-screen.
- The submit button collapses to a 46x44 icon button so the query keeps the width.
- Time and engine filters become two single-line swipeable strips with a right-edge
  mask fade. Wrapped chips used up ~300px of vertical space above the first result.
- Inputs and selects are 16px, which stops iOS from zooming on focus.
- URLs are single-line ellipsised and snippets clamp to three lines.
- Pagination targets are 40x40.
- "Open results in a new tab" is a preference, not a default: result links carry
  `target="_blank" rel="noopener"` only when `ms_newtab` is set. The click beacon still
  fires, because `sendBeacon` runs in the capture phase before the tab is handed off.

Verified with `scrollWidth == clientWidth` at 320, 360, 390 and 1280px.

## Kagi-derived datasets

Two MIT-licensed datasets from Kagi's open-source org, both refreshed weekly by
`trivet-data.timer` and at service start if stale:

**Small Web** (`smallweb.py`) — ~42,900 curated personal-blog domains from
`kagisearch/smallweb`. Membership adds +0.15 to the domain prior and lifts the
publisher trust used by the SEO classifier to 0.70. This is the "promote independent
human sites" half of Kagi's published policy, which the ranker previously had no
signal for. Exact-host matching only; subdomains are separate sites.

**Bangs** (`bangs.py`) — 13,600 triggers from `kagisearch/bangs`, the same list Kagi
uses, so `!gh`, `!w`, `!yt` behave as expected. Leading and trailing placement both
work:

```
!gh rust ownership      -> https://github.com/search?q=rust+ownership
fedora !w               -> wikipedia search for "fedora"
!so                     -> https://stackoverflow.com/
```

A bang is resolved before any engine is called: the HTML route returns a 302 and
`/api/search` returns `{"bang": {...}, "results": []}`, so a handoff never costs
engine credits. Database state is visible in `/healthz` under `datasets`.

Costs and caveats: two ~1.5MB downloads a week, 4.7µs per Small Web lookup, and the
list is a curation, not a quality guarantee — it contains multi-author platforms
where individual feeds were submitted, so membership is a nudge rather than a verdict.

## SEO listicles and roundup spam

A listicle is not the problem; a listicle from a publisher with no standing is. So the
signal is an interaction, computed in `quality.py` from data the engines already
returned (no page fetches):

    penalty = smell × (1 − publisher_trust)

    smell   = 0.60·title_markers + 0.25·url_shape + 0.15·affiliate_boilerplate

- **title markers** — "10 Best X", "Top 5", a year, "ranked/reviewed/tested", or
  guide-spam phrases ("ultimate", "everything you need to know", "we tested").
- **url shape** — dated blog paths, long keyword slugs, a year in the slug, or a slug
  containing best-/top-/ranked. The slug counts even when the title is generic
  ("Web Browsers for Android" living at `/blogs/best-android-browsers-2025/`).
- **boilerplate** — affiliate and roundup language in the snippet ("we may earn",
  "our team tested", "updated for 2026").
- **publisher trust** — 1.0 reference docs and vendor sites, 0.85 established tech
  and consumer publishers (ZDNet, Ars Technica, RTINGS, PCMag, Wirecutter, ...), 0.6
  community sites, 0.35 unknown hosts, 0.15 discounted TLDs.

Weighted at 0.40 in `ranking.json` penalties. A ZDNet roundup keeps almost all of its
score (0.06 penalty); the same title on an unknown host loses about 0.35.

Measured on the frozen 22-query set: nDCG@10 went 0.752 → 0.794 and MRR 0.716 →
0.798, with plain RRF at 0.565. On `Android Web browsers` the top six are now ZDNet,
Android Police, XDA, GitHub, Play Store and How-To Geek; the listicles that were
second and fourth are out of the top ten.

Known failure mode: a good publisher that is not on the trust list gets treated as
unknown. RTINGS scored 0.33 before being added. The list needs occasional curation,
which is why it lives in code and not in a query.

Next levers, not built: fetching the top few results to measure content-to-ad ratio
and affiliate link density, domain age via RDAP, and site-level repetition (one host
holding several of the top ten).

## Intent and freshness

Ranking treats "software choice" differently from "software fact", because the two
have different shelf lives:

| Query shape | Example | Freshness behaviour |
|---|---|---|
| tech + choice | `Android Web browsers`, `best VPN`, `hyprland vs sway` | staleness penalized; encyclopedias demoted |
| tech + fact | `fedora 44 release date`, `python asyncio create_task` | freshness off; old docs are still correct |
| recency | `latest kernel news` | freshness weighted normally |

Signals added for this:

- **Choice detection** — explicit words (`best`, `top`, `vs`, `which`) or a category
  noun (`browsers`, `apps`, `clients`, `tools`). A plain concept query must not
  qualify, or freshness demotes the canonical page.
- **Encyclopedia demotion** (0.90, choice queries only) — Wikipedia answers "what is
  a browser" well and "which browser should I use" badly. Gated so
  `fedora 44 release date` still gets the trusted-domain boost.
- **Staleness penalties** — tech pages older than ~18 months lose 0.20, older than
  three years lose 0.32. Undated pages are not punished: engine dates are often the
  first publication of a live page, and penalising missing metadata would be punishing
  the wrong thing. (0.10 was too weak: a two-year-old roundup still outranked a
  current one.)
- **Year-claim penalty** — a page advertising "in 2024" for a current choice query
  loses 0.15 (0.22 if older). The claim is read from the title first, then from the
  slug, because listicles often carry the year only in the URL.

A *positive* freshness weight was tried again after the SEO penalty landed and still
measured worse (0.787 -> 0.775 nDCG at 0.03). It is pinned at 0.0: currency is enforced
by penalising age, not by rewarding novelty, because novelty is the cheaper thing to
fake.

One honest limit: the labelled set scores *relevance* (is the right page in the top 10),
not currency, and some of its reference answers are themselves older articles. So the
currency preference is a product judgement that costs about 0.007 nDCG here, not a
result the harness can confirm.

### Frozen evaluation sets

Live evaluation refetches from engines whose results drift: identical code scored
0.579 and 0.789 nDCG on consecutive runs. `--save-snapshot` writes the candidate set
to disk and `--snapshot` evaluates against it, so an A/B of two weight sets is
reproducible.

```bash
env -u PYTHONPATH python3 eval_rank.py --save-snapshot eval/snapshot-YYYY-MM-DD.json
env -u PYTHONPATH python3 eval_rank.py --snapshot eval/snapshot-YYYY-MM-DD.json
```

Frozen 22-query set: ranker 0.752 nDCG@10 / 0.716 MRR, plain RRF 0.565 / 0.491.

## Ranking

RRF fuses engine ranks and nothing else: it never reads the query, the title, the
source or the date. A second stage (`ranking.py`) scores each candidate on
independent signals and orders by the weighted sum:

| Signal | Weight | What it captures |
|---|---|---|
| rrf | 0.25 | multi-engine consensus (normalized to 1.0 for the top candidate) |
| domain | 0.22 | reference hosts and vendor docs vs UGC vs discounted TLDs |
| rank | 0.10 | best rank any single engine gave it |
| title | 0.10 | query-term coverage, complete-coverage bonus, keyword-stuffing discount |
| snippet | 0.05 | query terms in the snippet |
| fresh | 0.05 | exponential decay, applied strongly only for recency intent |
| quality | 0.05 | snippet length sanity |
| agree | 0.04 | share of engines that returned it |

Penalties subtract for spam-shaped hosts, titles under 15 characters, a page
older than a year named in the query, and thin snippets.

Two intent rules matter more than the weights. `release date`, `history`, `when`
and `timeline` are fact lookups, so freshness is switched off; only `latest`,
`news`, `today`, `changelog` and similar turn it on. And a domain counts as
reference only if it is not a UGC subdomain: `gist.github.com`,
`discussion.fedoraproject.org` and `community.cloudflare.com` are not the same
thing as `github.com` or the docs.

Results are also diversified: at most two per domain inside the top of the list,
with the overflow appended in score order, so one site cannot own the first screen.

### Page type beats domain

A domain is not uniform. Two path-aware rules matter more than any weight:

- **GitHub**: a repo or file is reference material (+0.80), an issue, pull request or
  discussion is a forum post (+0.15). Before this, an ungoogled-chromium issue titled
  "Plans to introduce Chrome Sync" outranked the actual Chrome Web Store listings for
  "history bookmark sync extension chrome".
- **Storefronts**: an official app or extension listing is the primary source for a
  "which extension" query. `chromewebstore.google.com/detail`, `chrome.google.com/webstore`,
  `addons.mozilla.org/*/firefox/addon`, `microsoftedge.microsoft.com/addons`,
  `apps.apple.com/*/app` and `play.google.com/store/apps` get +0.45 and a 0.90 trust
  tier, because those domains previously scored zero while a random blog scored zero too.

### Tuning and evaluation

`eval_rank.py` scores a ranker against labelled queries with nDCG@10 and MRR.
`tune_rank.py` grid-searches four weights against the same set.

```bash
env -u PYTHONPATH python3 eval_rank.py                     # rrf vs v1, 18 queries
env -u PYTHONPATH python3 eval_rank.py --queries eval/holdout.json
env -u PYTHONPATH python3 tune_rank.py                     # 81 combos
```

| Set | RRF nDCG@10 | v1 nDCG@10 | RRF MRR | v1 MRR |
|---|---|---|---|---|
| tuning (18 queries) | 0.636 | **0.792** | 0.563 | **0.797** |
| holdout (4 unseen) | 0.459 | **0.511** | 0.411 | 0.411 |

The first hand-picked weight set scored *worse* than plain RRF (0.564 vs 0.579);
tuning fixed that, and the holdout confirmed the tuned set generalizes rather than
memorizing the tuning queries.

`/debug/rank?q=...` renders every candidate with its per-signal contributions and
its movement versus raw RRF, using the same candidate depth as the results page.

Override weights without editing code by dropping
`~/.config/trivet/ranking.json` (merged over the defaults, reloaded when the
file's mtime changes):

```json
{"weights": {"domain": 0.18, "title": 0.14}}
```

Limits worth stating plainly: 22 labelled queries is small, ground truth is URL
substring matching against canonical pages, and no click data has been collected
yet. Re-tune only after adding queries, or the weights fit the sample.

## Domain reputation indicators

Results whose host is a known ad, tracker, malware or spam domain get a visible badge
with the reason in its tooltip. They are flagged, not hidden: a blocklist hit is
evidence about the host, not proof the page is worthless.

Three sources, cheapest first (`reputation.py`):

1. **Local lists** — `data/domains.json` (curated seed, in the repo) plus
   `~/.config/trivet/domains.json` (yours). Keys: `spam`, `suspicious`, `allow`.
   An entry in `allow` always wins, including over a blocklist hit.
2. **AdGuard Home** — `GET /control/filtering/check_host` against the instance already
   running on this box, which carries AdGuard DNS filter, Hagezi Multi Pro
   (ads/tracking/telemetry) and Hagezi Threat Intelligence (malware/phishing), about
   1.7M rules maintained upstream. A threat-intel match is `malware`; any other
   filtered match is `ads`, and the badge names the filter that matched.
3. **Host shape** — SEO-shaped hostnames (`bestvpn2026reviews.com`), warez tokens
   (`keygen`, `warez`, `nulled`) and link-selling hosts. No DNS list can know these.

Penalties: `malware` 0.40, `spam` 0.22, `ads` 0.10, `suspicious` 0.08, all tunable in
`ranking.json`.

Reputation answers are cached for 7 days in the `domain_rep` table, keyed by host. The
local lists are checked *before* the cache so adding a domain takes effect immediately
instead of waiting for a stale "not filtered" answer to expire. A full page of new
domains costs about 0.2s on first look and nothing afterwards.

Two deliberate restraints. A domain is not flagged just because the query is
commercial: someone searching "buy backlinks" wants the marketplaces that a naive rule
would punish. And host heuristics match only unambiguous tokens — a bare `crack`
pattern flagged cracked.com, a legitimate site, so the warez rule now needs a host
token or a download path.

## Feedback loop

The ranker is tuned against 22 hand-labelled queries, which is the ceiling of what
static evaluation can tell you. Real clicks are the better signal, so the engine logs
them (`feedback.py`, same SQLite file as the cache).

Three event shapes, borrowed from how purpose-built ranking services model this:

- **impression** — one row per shown position: query, ranker, engine set, url,
  domain, score, and the feature vector that produced the position
- **click** — which impression position was opened, recorded by a
  `navigator.sendBeacon` on the result link before navigation
- **feature snapshot** — stored with the impression so a different weight set can be
  replayed later without paying the engines again

Why impressions and not just clicks: a click without its impression cannot be
attributed to a position, and position bias lives in the impression. Raw CTR
therefore overstates the top of the page, which is why the report prints an
examination-weighted estimate alongside it (`1/(1+0.85·(pos−1))`) and withholds it
until there are enough clicks to be stable.

Rates are aggregated **per domain, smoothed toward a prior** (0.35 CTR, 8 pseudo
impressions). Per-URL rates are meaningless at one user and a few hundred URLs, and
without smoothing a domain with one impression would swing between 0% and 100%.

```bash
env -u PYTHONPATH python3 analyze_feedback.py                      # position + domain report
env -u PYTHONPATH python3 analyze_feedback.py --replay eval/tuned-v1b.json
```

`--replay` re-scores every page that has a click from its stored feature vectors and
reports MRR of the clicked result under each config. That is an offline A/B test on
real behaviour, no traffic needed.

Privacy: the log holds queries and the URLs you opened, on this machine only. It is
not sent anywhere.

## Which engines are worth it

`eval/combo_bench.py` fetches every labelled query against every engine **once**, freezes the
result to `eval/combo-snapshot.json`, then scores all subsets offline. The snapshot behind the
numbers below is pinned at `eval/combo-snapshot-2026-11-09.json`; a fetch takes an exclusive lock
and writes through a temp file, so two runs cannot interleave into one half-written snapshot. Refetching per subset
would compare different result sets.

| Engine alone | nDCG@10 | mean latency |
|---|---|---|
| Serper (Google) | 0.625 | 1.30s |
| Brave | 0.563 | 0.18s |
| Exa | 0.549 | 0.11s |
| Tavily | 0.519 | 0.03s |
| Keenable | 0.500 | 0.19s |
| Linkup | 0.255 | 0.80s |
| Firecrawl | 0.253 | 0.84s (38.5% HTTP 429) |

Recommended set: **Keenable + Exa + Brave + Serper** — 96.2% recall@20, nDCG 0.709, ~1.3s.
That is the set that maximises recall inside the latency budget: Tavily and Linkup grow the
candidate pool (67 and 82 URLs vs 55) without surfacing a labelled answer the four missed.
The three-engine set Exa+Brave+Serper scores highest on nDCG alone (0.735) but drops recall to
92.3%, and dropping Keenable is not worth one query in 26.

Method notes that matter: run engines through `run_engine`, not `spec.fn`, or the benchmark
measures your own burst rate (Brave 429'd 9/26, Firecrawl 16/26). Score recall as well as nDCG —
ranking quality on labels says nothing about whether the answer exists in the pool at all.

## Deep engines and latency

Valyu is an agentic retriever: `/v1/search` fetches and extracts page content server-side, so
its payload is 30-140KB and a cold call takes ~5-6 seconds (measured: n=3 → 5.7s, n=10 → 5.0s,
so result count is not the cost). Repeated identical queries return in ~0.12s from Valyu's own
cache. There is no fast mode on `/v1/search`; the `instant`/`fast`/`standard` modes in their docs
belong to DeepResearch, a different endpoint.

Because the app renders only after every engine answers, an always-on Valyu put six seconds on
every query. Now the fast tier runs first and the deep tier runs afterwards only when the fast
tier came up short — a fast engine failed, or the merged set could not fill a page — unless
Preferences is set to `always`. Engines are grouped by tier in Preferences, each group heading carrying a `?` that explains the
trade on hover or focus. A skipped deep engine is marked `skipped`: it is not a failure,
it is not recorded in the speed stats, it does not block caching, and the timing strip says
"Valyu skipped" instead of inventing a duration.

## Engine speed

Two views of the same measurement, both server-side wall clock:

- **Per query** — a line under the result count on every results page:
  `Keenable 201ms · Exa 1.9s · Brave 730ms · Tavily 1.2s`. Failures show their reason
  instead of a time. Cached pages say "timings from the original fetch" so a fast page
  is not mistaken for fast engines.
- **`/stats`** — p50, p95, mean, max and success rate per engine over the last 24 hours
  or 7 days, with a p95 bar scaled to the slowest engine. Linked from the footer and
  the home page.

Samples go to an `engine_timings` table in the same SQLite file, one row per engine per
search, pruned after 45 days. Cache hits are not recorded: replaying a stored result
would double-count one fetch and flatter the engines. The table was seeded once from
the query cache so the page had history on day one (492 rows), and Valyu's numbers are
visibly worse because it is the opt-in deep engine, not because its calls are slow for
everyone.

## Cache

Every search bills each engine, so results are stored in SQLite at
`~/.cache/trivet/cache.db` (WAL, 15 min TTL by default, 1000-row cap).

- **Key** = normalized query + engine set + time range + safesearch + result depth.
  Normalization folds case and whitespace, so `Cache   TEST query` hits the same row.
- **Only fully successful runs are stored.** If an engine 401s or times out, nothing
  is written: caching that run would replay the failure for the whole TTL and hide a
  key that had recovered.
- **Depth is part of the key**, so page 2 has its own entry rather than re-billing
  page 1's results at a deeper fetch.
- `?nocache=1` bypasses the cache, and the results page shows a `refresh` link that
  sets it.
- The page reports `cached 4m ago` instead of a fake millisecond timing, and
  `/healthz` returns entry count and TTL.

Measured: repeat query 0.002 s instead of 0.4-2.1 s, and the cache survives a
service restart.

## Performance

Warm, three fast engines in parallel: 150-400 ms per query. The first query after a
restart pays TLS handshakes to each engine (1.5-2 s), which is why the process keeps
one pooled `httpx.AsyncClient` instead of building one per request. Valyu cold-starts
at 4-6 s and is off by default for that reason; when enabled it gets a 10 s budget.

## Files

- `cache.py` — SQLite query cache, normalization, TTL and pruning
- `providers.py` — engine adapters, env/Infisical loading, per-engine timeout and error isolation
- `merge.py` — URL canonicalization, RRF, consensus metrics
- `render.py` — server-rendered HTML (home, results, preferences, about), CSS, favicon
- `app.py` — FastAPI routes, cookies, pagination, CLI

## Known gaps

- Web results only: no image, news, or video categories (the backends do not offer a
  comparable media index)
- No bangs (`!w`) or query suggestions
- Pagination asks each engine for `page × per` results, capped at 50
- Safe search is per-engine, not enforced globally
- Runs as a user process; no systemd unit or HTTPS tunnel yet
