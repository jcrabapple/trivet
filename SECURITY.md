# Security

## Reporting a vulnerability

Open a private security advisory on the repository, or email the maintainer directly if
that is not possible. Please do not open a public issue for an exploitable report. Expect
an acknowledgement within a few days.

## What this app assumes

- **Every query spends the operator's money.** Each one fans out to commercial search
  APIs. A public instance without credentials is somebody else's free search engine, so
  the process refuses to start on a non-loopback address unless `TRIVET_USER` and
  `TRIVET_PASS` are both set.
- **API keys are the crown jewels.** Keys are stored encrypted (AES-256-GCM, HKDF from
  `TRIVET_SECRET_KEY`) and are write-only in the interface: the page shows the last four
  characters and nothing else. Keys in environment variables are the operator's
  responsibility.
- **Search results are hostile input.** Titles, snippets and URLs come from third-party
  indexes. They are escaped for HTML, and only `http(s)` URLs reach an `href`; anything
  else is dropped when the payload is parsed, not only when it is rendered.
- **The reverse proxy is trusted, clients are not.** Forwarding headers are believed only
  when the immediate peer is loopback.

## Defence in depth

Every response carries a Content-Security-Policy with `default-src 'none'`,
`frame-ancestors 'none'` and `base-uri 'none'`, plus `nosniff` and `no-referrer`. The
page loads no external assets.

Sessions are signed cookies (HMAC-SHA256) with an issue time; set `TRIVET_SESSION_EPOCH`
to a later timestamp to evict every existing session without rotating the key that
protects stored credentials.

## Known, accepted limitations

- `!bang` queries redirect off-site (`/search?q=!w anything`). That is the feature. A
  public instance will be usable as a redirect for links to arbitrary sites.
- `/logout` and `/preferences?save=1` are state-changing GETs. The effect is limited to
  the viewer's own browser and is trivially reversible.
- The bang table is downloaded from a third party at startup. It is pinned by URL, not
  by hash.

A full pre-release review, including the fixes applied in response, is in
`docs/SECURITY-REVIEW.md`.
