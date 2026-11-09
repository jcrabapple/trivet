"""HTML rendering for the human-facing search UI.

Server-rendered on purpose: the page works with JS off, every result state is a
URL you can bookmark or share, and there is no build step.
"""
from __future__ import annotations

import time
from datetime import datetime
from html import escape
from urllib.parse import urlencode, urlsplit

from merge import MergedHit
from providers import EngineResult, RECOMMENDED, RECOMMENDED_CAVEAT, RECOMMENDED_NOTE, Spec

TIME_LABELS = (("any", "Any time"), ("day", "Past day"), ("week", "Past week"),
               ("month", "Past month"), ("year", "Past year"))

CSS = """
:root{
  --bg:#fff; --fg:#1b1d21; --muted:#6b7280; --line:#e3e6ea; --field:#fff;
  --link:#1a4fd6; --visited:#6b3fb5; --chip:#f3f5f8; --accent:#1a4fd6;
  --danger:#b42318; --ok:#127a4b;
}
html[data-theme="dark"]{
  --bg:#15171b; --fg:#e7e9ee; --muted:#9aa3b2; --line:#2a2f38; --field:#1d2027;
  --link:#8ab4f8; --visited:#c0a8f0; --chip:#1f232b; --accent:#8ab4f8;
  --danger:#ff9a9a; --ok:#6ee7a8;
}
@media (prefers-color-scheme:dark){
  html:not([data-theme="light"]){
    --bg:#15171b; --fg:#e7e9ee; --muted:#9aa3b2; --line:#2a2f38; --field:#1d2027;
    --link:#8ab4f8; --visited:#c0a8f0; --chip:#1f232b; --accent:#8ab4f8;
    --danger:#ff9a9a; --ok:#6ee7a8;
  }
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg)}
body{font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
a:visited{color:var(--visited)}
.wrap{max-width:760px;margin:0 auto;padding:0 20px}
form.search{display:flex;gap:8px;align-items:center}
form.search input[type=search]{flex:1;background:var(--field);border:1px solid var(--line);
  color:var(--fg);padding:11px 14px;border-radius:24px;font-size:15px;outline:none;min-width:0}
form.search input[type=search]:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent)}
form.search button{background:var(--accent);color:#fff;border:0;border-radius:24px;
  padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;gap:6px}
form.search .btn-icon{display:none;width:19px;height:19px}
/* dark ink only when the accent is the light dark-mode blue; the default light
   theme has a deep blue accent and needs white text */
html[data-theme="dark"] form.search button,
html[data-theme="dark"] .prefs button{color:#0b0d10}
@media (prefers-color-scheme:dark){
  html:not([data-theme="light"]) form.search button,
  html:not([data-theme="light"]) .prefs button{color:#0b0d10}
}
.brand{font-size:20px;font-weight:600;letter-spacing:-.02em;color:var(--fg);display:inline-flex;align-items:center;gap:7px;
  font-family:ui-monospace,SFMono-Regular,Cascadia Mono,Roboto Mono,Liberation Mono,DejaVu Sans Mono,monospace}
.brand .mk{width:1.05em;height:1.05em;fill:var(--accent);flex:0 0 auto}
.brand:hover{text-decoration:none}
.brand span{color:var(--accent)}
.home .brand .mk{width:1em;height:1em}
header.top{border-bottom:1px solid var(--line);padding:14px 0}
header.top .inner{display:flex;gap:18px;align-items:center}
header.top form.search{flex:1}
header.top .nav{display:flex;gap:14px;font-size:13px;color:var(--muted);white-space:nowrap}
header.top .nav a{color:var(--muted)}
.home{padding:16vh 0 0;text-align:center}
.home .brand{font-size:38px;display:block;margin-bottom:6px}
.home .tag{color:var(--muted);font-size:14px;margin-bottom:26px}
.home form.search{max-width:560px;margin:0 auto}
.home .engines{margin-top:26px;font-size:13px;color:var(--muted);line-height:1.9;
  display:block;text-align:center}
.home .engines a{color:var(--muted);text-decoration:underline;text-decoration-color:var(--line)}
.home .engines b{color:var(--fg);font-weight:600}
.meta{display:flex;justify-content:space-between;gap:12px;color:var(--muted);
  font-size:13px;margin:16px 0 6px;flex-wrap:wrap}
/* only the phrases inside the count line are unbreakable; the count line itself
   must wrap, or a 320px viewport overflows by its width */
.meta .count span{white-space:nowrap}
.meta .hit{color:var(--ok)}
.meta .refresh{color:var(--muted);text-decoration:underline;
  text-decoration-color:color-mix(in srgb,var(--muted) 55%,transparent);
  text-underline-offset:2px;font-size:12px}
.filters{display:flex;gap:0;align-items:center;flex-wrap:wrap;margin:10px 0 18px;
  padding-bottom:12px;border-bottom:1px solid var(--line);font-size:13px}
.fgroup{display:flex;align-items:center;gap:6px;margin-right:16px}
.fgroup .strip{display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.fgroup:last-child{margin-right:0}
.filters .lbl{color:var(--fg);opacity:.55;margin-right:4px;font-size:12px;
  text-transform:uppercase;letter-spacing:.04em}
.filters a{padding:4px 10px;border-radius:14px;color:var(--muted);
  border:1px solid transparent}
.filters a:hover{background:var(--chip);text-decoration:none}
.filters a.on{background:var(--chip);color:var(--fg);font-weight:600;
  border-color:var(--line)}
.filters .sep{width:1px;height:16px;background:var(--line);margin:0 8px}
article.res{padding:14px 0 16px;border-bottom:1px solid var(--line)}
article.res:last-of-type{border-bottom:0}
.src{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted);margin-bottom:3px;flex-wrap:wrap}
.src .dom{color:var(--fg);font-weight:500}
.src .engines{margin-top:0}
article.res h2{margin:0 0 2px;font-size:18px;font-weight:600;line-height:1.35}
article.res h2 a{color:var(--link)}
article.res .url{color:var(--muted);font-size:12.5px;margin-bottom:6px;word-break:break-all}
article.res p{margin:0;color:var(--fg);opacity:.92;font-size:14px}
.engines{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap;align-items:center}
.e{font-size:10.5px;color:var(--muted);border:1px solid var(--line);border-radius:10px;
  padding:0 6px;line-height:1.6}
.e.agree{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}
.flag{font-size:10.5px;border-radius:9px;padding:0 6px;line-height:1.6;
  border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.flag[data-cat="malware"],.flag[data-cat="spam"]{color:var(--danger);
  border-color:color-mix(in srgb,var(--danger) 45%,transparent)}
.flag[data-cat="ads"],.flag[data-cat="suspicious"]{color:#b45309;
  border-color:color-mix(in srgb,#b45309 40%,transparent)}
html[data-theme="dark"] .flag[data-cat="ads"],
html[data-theme="dark"] .flag[data-cat="suspicious"]{color:#e0a458}
.dupes{font-size:11.5px;color:var(--muted);margin-top:6px}
.pages{display:flex;gap:6px;justify-content:center;margin:28px 0 10px;font-size:14px}
.pages a,.pages span{padding:5px 11px;border-radius:6px;border:1px solid var(--line)}
.pages .cur{background:var(--chip);font-weight:600}
.errbox{border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:14px 0;
  font-size:12.5px;color:var(--muted)}
.errbox b{color:var(--danger);font-weight:600}
.none{padding:40px 0;color:var(--muted)}
footer{border-top:1px solid var(--line);margin-top:34px;padding:16px 0 40px;
  color:var(--muted);font-size:12.5px;display:flex;gap:16px;flex-wrap:wrap}
footer a{color:var(--muted)}

/* ---------------- phone layout ---------------- */
@media (max-width: 640px){
  .wrap{padding:0 14px}
  /* header: brand + links on row 1, full-width search on row 2. The desktop
     single row is 637px wide at a 390px viewport, which pushed the nav links
     off-screen entirely. */
  header.top{padding:10px 0 12px}
  header.top .inner{flex-wrap:wrap;gap:8px 12px;align-items:center}
  header.top .brand{order:1;font-size:18px}
  header.top .nav{order:2;margin-left:auto;gap:18px;font-size:13px}
  header.top .nav a{padding:6px 0}
  header.top form.search{order:3;flex:1 1 100%;width:100%}
  form.search input[type=search]{flex:1 1 100%;min-width:0;font-size:16px;
    padding:11px 14px;border-radius:22px}
  /* icon-only submit is for the header, where the query needs the width.
     The login form reuses .search and must keep its label. */
  header.top form.search button{flex:0 0 auto;width:46px;height:44px;padding:0;
    border-radius:22px}
  header.top form.search .btn-icon{display:block}
  header.top form.search .btn-text{position:absolute;width:1px;height:1px;overflow:hidden;
    clip-path:inset(50%);white-space:nowrap}
  .meta{margin:12px 0 4px;font-size:12.5px;gap:4px}
  /* filters become two swipeable strips instead of four wrapped rows; chips keep
     a 36px touch height while the row stays one line tall */
  .filters{display:block;padding-bottom:10px;margin:6px 0 14px}
  .fgroup{display:flex;align-items:center;gap:8px;margin:6px 0;width:100%}
  .fgroup .lbl{flex:0 0 auto;margin:0;font-size:11px}
  /* same specificity as the desktop rule above, and later in the file, so this
     is what actually wins: one swipeable line instead of wrapped rows */
  .fgroup .strip{display:flex;flex-wrap:nowrap;gap:6px;overflow-x:auto;
    scrollbar-width:none;-webkit-overflow-scrolling:touch;padding:1px 0;
    flex:1 1 auto;min-width:0;
    -webkit-mask-image:linear-gradient(to right,#000 calc(100% - 34px),transparent);
    mask-image:linear-gradient(to right,#000 calc(100% - 34px),transparent)}
  .fgroup .strip::-webkit-scrollbar{display:none}
  .filters a{flex:0 0 auto;display:inline-flex;align-items:center;min-height:36px;
    padding:0 12px;font-size:13.5px}
  article.res{padding:13px 0 15px}
  article.res h2{font-size:17px;line-height:1.3}
  article.res .url{font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  article.res p{font-size:14px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
    overflow:hidden}
  .src{font-size:12px}
  .e{font-size:10px}
  .pages{gap:8px;margin:22px 0 8px}
  .pages a,.pages span{min-width:40px;min-height:40px;display:inline-flex;
    align-items:center;justify-content:center;padding:0 10px}
  .prefs{margin:20px auto 40px}
  .prefs label{padding:9px 0;gap:10px}
  .prefs select{font-size:16px}          /* iOS zooms any focused control under 16px */
  .prefs button{width:100%;padding:14px 18px;font-size:15px;border-radius:10px}
  .prefs .sub{font-size:11.5px}
  footer{padding:14px 0 32px;gap:12px}
}
@media (prefers-reduced-motion: no-preference){
  .strip{scroll-behavior:smooth}
}

.prefs{max-width:560px;margin:26px auto 60px}
.prefs h1{font-size:20px;margin:0 0 18px}
.prefs fieldset{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 16px}
.prefs legend{padding:0 6px;font-size:13px;color:var(--muted)}
.prefs label{display:flex;gap:9px;align-items:center;padding:5px 0;font-size:14px}
.prefs .sub{font-size:12px;color:var(--muted);margin-left:auto}
.prefs button{background:var(--accent);color:#fff;border:0;border-radius:8px;
  padding:10px 18px;font-weight:600;cursor:pointer}
html[data-theme="dark"] .prefs button{color:#0b0d10}
.kbd{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;
  padding:0 5px;margin-left:8px}
"""


def _fmt_date(raw: str | None) -> str:
    """Format engine dates without mangling the ones we cannot parse.

    Brave sends human strings ("August 18, 2025") while Exa and Valyu send ISO
    timestamps. Slicing unknown formats truncated the year, so unparsed values are
    returned whole.
    """
    if not raw:
        return ""
    text = str(raw).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d",
                "%B %d, %Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%d %b %Y")
        except ValueError:
            continue
    return text


def layout(title: str, body: str, theme: str = "", impression_id: str = "") -> str:
    theme_attr = f' data-theme="{escape(theme)}"' if theme in ("light", "dark") else ""
    return f"""<!doctype html>
<html lang="en"{theme_attr}><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="search" type="application/opensearchdescription+xml" title="trivet"
      href="/opensearch.xml">
<style>{CSS}</style></head>
<body data-imp="{escape(impression_id)}">{body}
<script>
// click beacon: send before navigation so the click is attributed to this
// impression and position; capture phase runs before the anchor navigates
const sendClick = (el) => {{
  const imp = document.body.dataset.imp;
  if(!imp || !el || !el.dataset.pos) return;
  const payload = JSON.stringify({{impression_id: imp, position: +el.dataset.pos, url: el.href}});
  if (navigator.sendBeacon) {{
    navigator.sendBeacon('/api/click', new Blob([payload], {{type: 'application/json'}}));
  }} else {{
    fetch('/api/click', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:payload, keepalive:true}});
  }}
}};
const resultLink = (e) => (e.target.closest && e.target.closest('article.res h2 a'));
document.addEventListener('click', e => sendClick(resultLink(e)), true);
document.addEventListener('auxclick', e => sendClick(resultLink(e)), true);
addEventListener('keydown',e=>{{
  if(e.key==='/'&&document.activeElement.tagName!=='INPUT'){{
    const i=document.querySelector('input[type=search]'); if(i){{e.preventDefault();i.focus();i.select();}}
  }}
  if(e.key==='Escape'){{const i=document.querySelector('input[type=search]'); if(i) i.blur();}}
}});
</script></body></html>"""


def _search_form(q: str) -> str:
    return f"""<form class="search" action="/search" method="get" role="search">
  <input type="search" name="q" value="{escape(q)}" placeholder="Search, or !bang a site"
         autocomplete="off" spellcheck="false" aria-label="Search query">
  <button type="submit" aria-label="Search">
    <svg class="btn-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"
      fill="none" stroke="currentColor" stroke-width="2.2"/><line x1="16" y1="16" x2="21" y2="21"
      stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>
    <span class="btn-text">Search</span>
  </button>
</form>"""


def home(enabled: list[Spec], configured: list[Spec], theme: str = "") -> str:
    names = " · ".join(escape(s.label) for s in enabled)
    off = len(configured) - len(enabled)
    more = (f' · <a href="/preferences">{off} more available</a>' if off > 0 else "")
    body = f"""<div class="wrap"><div class="home">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  <div class="tag">A search engine over agentic indexes.</div>
  {_search_form("")}
  <div class="engines">{f'<b>{len(enabled)} engine{"" if len(enabled) == 1 else "s"}</b> · {names}{more}'
    if enabled else '<b>No engines yet.</b> <a href="/settings/keys">Add an API key</a> to start searching.'}</div>
  <footer style="justify-content:center;border:0;margin-top:26px">
    <a href="/preferences">Preferences</a><a href="/stats">Engine stats</a>
    <a href="/about">About</a>
  </footer>
</div></div>"""
    return layout("trivet", body, theme)


def _filters(q: str, engines_on: set[str], all_engines: list[Spec],
             time_range: str, ss: str, per: int) -> str:
    def base(**over):
        params = {"q": q, "engines": ",".join(sorted(engines_on)), "time": time_range,
                  "ss": ss, "per": per}
        params.update(over)
        return "/search?" + urlencode({k: v for k, v in params.items() if v})

    times = "".join(
        f'<a class="{"on" if time_range == key else ""}" href="{escape(base(time=key))}">'
        f'{label}</a>' for key, label in TIME_LABELS)

    chips = []
    for spec in all_engines:
        on = spec.name in engines_on
        nxt = set(engines_on)
        if on and len(engines_on) > 1:
            nxt.discard(spec.name)
        elif not on:
            nxt.add(spec.name)
        chips.append(
            f'<a class="{"on" if on else ""}" href="{escape(base(engines=",".join(sorted(nxt))))}" '
            f'title="{escape(spec.blurb)}">{escape(spec.label)}</a>')
    return (f'<div class="filters">'
            f'<div class="fgroup"><span class="lbl">Time</span>'
            f'<div class="strip">{times}</div></div>'
            f'<div class="fgroup"><span class="lbl">Engines</span>'
            f'<div class="strip">{"".join(chips)}</div></div>'
            f'</div>')


FLAG_LABELS = {"malware": "malware / phishing", "spam": "spam",
               "ads": "ad domain", "suspicious": "suspicious"}


def _flag(rep) -> str:
    """A visible warning on the result. A blocklist hit is evidence about the host,
    not a verdict on the page, so it is labelled and explained rather than removed."""
    if rep is None or not getattr(rep, "flagged", False):
        return ""
    label = FLAG_LABELS.get(rep.category, rep.category)
    detail = rep.detail or rep.source or ""
    return (f'<span class="flag" data-cat="{escape(rep.category)}" '
            f'title="{escape("Flagged as " + label + (" by " + detail if detail else ""))}">'
            f'{escape(label)}</span>')


def _result(hit: MergedHit, engines: dict[str, Spec], position: int = 0,
            rep=None, new_tab: bool = False) -> str:
    date = f' · {escape(_fmt_date(hit.published))}' if hit.published else ""
    # stable badge order (Spec order), so the same page always renders the same way
    order = {s.name: i for i, s in enumerate(engines.values())}
    badges = "".join(
        f'<span class="e{" agree" if hit.agreement > 1 else ""}">'
        f'{escape(engines[e].label)}</span>'
        for e in sorted(hit.engines, key=lambda n: order.get(n, 99)) if e in engines)
    # Same page, different query string: that is a URL variant, not a reprint, and
    # saying otherwise reads as a broken duplicate to a human.
    dupe = ""
    if len(hit.duplicates) >= 2:
        dupe = f'<div class="dupes">{len(hit.duplicates) + 1} URLs merged</div>' 
    return f"""<article class="res">
  <div class="src"><span class="dom">{escape(hit.domain)}</span>{date}{_flag(rep)}
    <span class="engines">{badges}</span></div>
  <h2><a href="{escape(_safe_href(hit.url))}" rel="noopener"{' target="_blank"' if new_tab else ''} data-pos="{position}">{escape(hit.title)}</a></h2>
  <div class="url">{escape(hit.url)}</div>
  {f'<p>{escape(hit.snippet)}</p>' if hit.snippet else ''}
  {dupe}
</article>"""


def results_page(*, q: str, hits: list[MergedHit], page: int, per: int,
                 total_unique: int, elapsed: float, results: list[EngineResult],
                 engines_on: set[str], all_engines: list[Spec], time_range: str,
                 ss: str, theme: str = "", error: str = "",
                 cached_age: float | None = None,
                 impression_id: str = "",
                 reps: dict | None = None,
                 new_tab: bool = False) -> str:
    by_name = {s.name: s for s in all_engines}
    failed = [r for r in results if not r.ok and r.error and r.error != "no key"]
    ok = [r for r in results if r.ok]

    fail_box = ""
    if failed:
        fail_box = '<div class="errbox"><b>Unavailable:</b> ' + " · ".join(
            f'{escape(by_name[r.name].label if r.name in by_name else r.name)} ({escape(r.error)})'
            for r in failed) + "</div>"

    noun = "engine" if len(ok) == 1 else "engines"
    answered = (f'all {len(ok)} {noun} answered' if len(ok) == len(engines_on)
                else f'{len(ok)} of {len(engines_on)} engines answered')
    # separate spans so a phone wraps between phrases, never inside one
    if cached_age is None:
        timing = f'<span>{elapsed:.2f}s</span>'
    else:
        age = "just now" if cached_age < 10 else f"{_human_age(cached_age)} ago"
        timing = f'<span class="hit">cached {age}</span>' 
    count_line = (f'<span>About {total_unique:,} results</span> · '
                  f'{timing} · <span>{answered}</span>')

    pager_bits = []
    if page > 1:
        pager_bits.append(f"page {page}")
    pager_bits.append(
        f'<a class="refresh" href="'
        f'{escape(_page_url(q, page, engines_on, time_range, ss, per) + chr(38) + "nocache=1")}" '
        f'title="Search the engines again instead of using the cache">↻ refresh</a>')
    pager_html = f'<span class="pager">{" · ".join(pager_bits)}</span>'

    reps = reps or {}
    body_hits = "".join(_result(h, by_name, (page - 1) * per + i, reps.get(h.domain),
                                new_tab)
                        for i, h in enumerate(hits, start=1))
    if not hits:
        body_hits = (f'<div class="none">No results for <b>{escape(q)}</b>. '
                     f'Try fewer words or a wider time range.</div>')

    # pagination
    pages = []
    if hits or page > 1:
        if page > 1:
            pages.append(f'<a href="{escape(_page_url(q, page - 1, engines_on, time_range, ss, per))}">Previous</a>')
        for p in range(max(1, page - 2), page + 3):
            if p == page:
                pages.append(f'<span class="cur">{p}</span>')
            elif p == 1 or p <= page + 2:
                pages.append(f'<a href="{escape(_page_url(q, p, engines_on, time_range, ss, per))}">{p}</a>')
        if len(hits) == per:
            pages.append(f'<a href="{escape(_page_url(q, page + 1, engines_on, time_range, ss, per))}">Next</a>')

    body = f"""<header class="top"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form(q)}
  <div class="nav"><a href="/preferences">Preferences</a><a href="/about">About</a><a href="/logout">Log out</a></div>
</div></header>
<div class="wrap">
  <div class="meta"><span class="count">{count_line}</span>{pager_html}</div>
  {_timings_line(results, cached_age, {s.name: s.label for s in all_engines})}
  {fail_box}
  {_filters(q, engines_on, all_engines, time_range, ss, per)}
  {body_hits}
  <div class="pages">{''.join(pages)}</div>
  <footer><span>trivet</span><span>{len(all_engines)} engines available</span>
    <span>RRF fusion</span><a href="/preferences">Preferences</a>
    <a href="/stats">Engine stats</a></footer>
</div>"""
    if error:
        body = body.replace('<div class="wrap">', f'<div class="wrap"><div class="errbox">{escape(error)}</div>', 1)
    return layout(f"{q} · trivet" if q else "trivet", body, theme,
                  impression_id)


def _safe_href(url: str) -> str:
    """Only http(s) links reach an href. html.escape does not touch a scheme, and
    a javascript: URL from a vendor payload would otherwise be a click away from
    running in this origin."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "#"
    return url if parts.scheme.lower() in ("http", "https") else "#"


def rate_limited(theme: str = "") -> str:
    body = f"""<div class="wrap"><div class="home" style="padding-top:18vh">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  <div class="tag">Too many searches. Wait a minute and try again.</div>
  <p style="color:var(--muted);font-size:13px;max-width:420px;margin:14px auto 0">
  Every query spends real money against a paid API, so the instance limits how
  many one client can issue per minute.</p>
  <p><a href="/">Back to search</a></p>
</div></div>"""
    return layout("Slow down · trivet", body, theme)


def _human_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _page_url(q, page, engines_on, time_range, ss, per) -> str:
    return "/search?" + urlencode({
        "q": q, "page": page, "engines": ",".join(sorted(engines_on)),
        "time": time_range, "ss": ss, "per": per})


def login(next_path: str = "/", error: str = "") -> str:
    err = (f'<p class="err">{escape(error)}</p>' if error else "")
    body = f"""<div class="wrap"><div class="home" style="padding-top:14vh">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  <div class="tag">Sign in to search.</div>
  <form class="search login" method="post" action="/login" style="max-width:420px;margin:0 auto">
    <input type="hidden" name="next" value="{escape(next_path)}">
    <input type="password" name="password" placeholder="Password" autofocus
           autocomplete="current-password" aria-label="Password" required>
    <button type="submit">Sign in</button>
  </form>
  {err}
  <p class="hint">Stays signed in on this browser for 90 days.</p>
</div></div>"""
    extra = """
.login input{background:var(--field);border:1px solid var(--line);color:var(--fg);
  padding:11px 14px;border-radius:24px;font-size:16px;flex:1;min-width:0}
.login button{flex:0 0 auto;white-space:nowrap;padding:11px 20px;border-radius:24px}
.home .err{color:var(--danger);font-size:13px;margin-top:14px}
.home .hint{color:var(--muted);font-size:12px;margin-top:22px}
"""
    return layout("Sign in · trivet", body, "").replace("</style>", extra + "</style>")


def _timings_line(results: list, cached_age: float | None,
                  labels: dict[str, str] | None = None) -> str:
    """One query's per-engine latency, which is the number people actually ask about."""
    if not results:
        return ""
    bits = []
    for r in results:
        label = (labels or {}).get(r.name, r.name)
        if getattr(r, "skipped", False):
            bits.append(f'<span class="skip">{escape(label)} skipped</span>')
        elif r.ok:
            ms = r.ms
            shown = f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"
            bits.append(f'{escape(label)} <b>{shown}</b>')
        else:
            bits.append(f'{escape(label)} <span class="fail">{escape(r.error or "failed")}</span>')
    note = " · timings from the original fetch" if cached_age is not None else ""
    return f'<div class="timings">{" · ".join(bits)}{note}</div>'


def stats_page(rows: list[dict], hours: int, labels: dict[str, str] | None = None) -> str:
    window = "last 24 hours" if hours <= 24 else "last 7 days"
    if not rows:
        body_rows = ('<tr><td colspan="7" style="color:var(--muted)">'
                     'No samples yet in this window.</td></tr>')
        peak = 1
    else:
        peak = max((r["p95"] for r in rows), default=1) or 1
        cells = []
        for r in rows:
            width = max(2, round(100 * r["p95"] / peak))
            cells.append(
                f"<tr><td><b>{escape((labels or {}).get(r['engine'], r['engine']))}</b></td>"
                f"<td>{r['samples']}</td><td>{r['p50']}ms</td><td>{r['p95']}ms</td>"
                f"<td>{r['mean']}ms</td><td>{r['max']}ms</td>"
                f"<td>{r['success']}%</td>"
                f"<td class='bar'><span style='width:{width}%'></span></td>"
                f"<td class='err'>{escape(r['last_error']) or '—'}</td></tr>")
        body_rows = "".join(cells)
    other = 168 if hours <= 24 else 24
    other_label = "7 days" if hours <= 24 else "24 hours"
    body = f"""<header class="top"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form("")}
  <div class="nav"><a href="/preferences">Preferences</a><a href="/about">About</a>
    <a href="/logout">Log out</a></div>
</div></header>
<div class="wrap"><div class="prefs" style="max-width:820px">
  <h1>Engine speed · {window}</h1>
  <p style="color:var(--muted);font-size:13px;margin-top:-6px">
    Wall-clock per engine call, measured server-side. Bar is p95 relative to the slowest engine.</p>
  <div class="tablewrap"><table class="stats"><thead><tr><th>Engine</th><th>calls</th>
    <th>p50</th><th>p95</th><th>mean</th><th>max</th><th>success</th><th>p95</th>
    <th>last error</th></tr></thead><tbody>{body_rows}</tbody></table></div>
  <p style="margin-top:14px;font-size:13px">
    <a href="/stats?hours={other}">Show {other_label}</a></p>
</div></div>"""
    extra = """
/* nine columns will not fit a phone; scroll the table rather than squeeze it */
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.stats{border-collapse:collapse;width:100%;min-width:640px;font-size:13px;
  font-variant-numeric:tabular-nums}
.stats th{text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--line);padding:6px 8px}
.stats td{padding:7px 8px;border-bottom:1px solid var(--line)}
.stats td.err{color:var(--danger);font-size:11.5px;max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stats td.bar{width:120px;border-bottom:1px solid var(--line)}
.stats td.bar span{display:block;height:7px;border-radius:4px;background:var(--accent);opacity:.75}
.timings{font-size:11.5px;color:var(--muted);margin:-2px 0 12px;font-variant-numeric:tabular-nums}
.timings b{color:var(--fg);font-weight:600}
.timings .fail{color:var(--danger)}
.timings .skip{color:var(--muted);opacity:.85}
"""
    return layout(f"Engine speed · trivet", body, "").replace("</style>", extra + "</style>")


def rank_debug(q: str, ranked: list, rrf: list, cfg: dict, cached: bool,
               reps: dict | None = None) -> str:
    """Side-by-side of the v1 ranker against raw RRF, with every feature exposed.

    Without this table, "the ordering is wrong" is an opinion. With it, the weight
    that produced the ordering is a number you can change.
    """
    rrf_pos = {h.hit.url: i for i, h in enumerate(rrf, start=1)}
    weights = cfg["weights"]
    reps = reps or {}
    head = "".join(f"<th>{k}</th>" for k in
                   ("rrf", "rank", "agree", "title", "snippet", "domain", "fresh",
                    "quality", "pen", "seo", "rep"))
    rows = []
    for i, item in enumerate(ranked, start=1):
        f = item.features
        move = rrf_pos.get(item.hit.url, "–")
        delta = (rrf_pos[item.hit.url] - i) if item.hit.url in rrf_pos else 0
        arrow = f'<span class="up">▲{delta}</span>' if delta > 0 else (
            f'<span class="down">▼{-delta}</span>' if delta < 0 else "")
        cells = "".join(f"<td>{getattr(f, k):.2f}</td>" for k in
                        ("rrf", "rank", "agree", "title", "snippet", "domain", "fresh",
                         "quality", "seo"))
        rows.append(
            f"<tr><td>{i}</td><td>{move} {arrow}</td><td>{item.score:.3f}</td>{cells}"
            f"<td class='pen'>{f.penalties:.2f}</td>"
            f"<td>{escape(f.reputation)}</td>"
            f"<td><a href='{escape(_safe_href(item.hit.url))}' rel='noopener'>{escape(item.hit.title[:78])}</a>"
            f"<div class='d'>{escape(item.hit.domain)} · {escape(str(f.intent))}"
            f"{' · ' + str(f.published_days) + 'd old' if f.published_days is not None else ''}</div></td></tr>")
    body = f"""<div class="wrap" style="max-width:1180px">
  <h1 style="font-size:19px;margin:22px 0 6px">Ranking: <b>{escape(q)}</b></h1>
  <p style="color:var(--muted);font-size:13px;margin:0 0 14px">
    {len(ranked)} candidates{' · cached candidates' if cached else ''} ·
    weights: {" · ".join(f"{k}={v}" for k, v in weights.items())}
  </p>
  <table class="rank"><thead><tr><th>#</th><th>rrf pos</th><th>score</th>{head}<th>result</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table>
  <p style="color:var(--muted);font-size:12px">rrf pos = position under plain RRF fusion.
  ▲ moved up under v1, ▼ moved down. pen = sum of penalties subtracted.</p>
</div>"""
    extra = """
.rank{border-collapse:collapse;width:100%;font-size:12.5px}
.rank th{text-align:left;font-weight:600;color:var(--muted);border-bottom:1px solid var(--line);padding:6px 8px}
.rank td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.rank td.pen{color:var(--danger)}
.rank .d{color:var(--muted);font-size:11.5px;margin-top:2px}
.rank .up{color:var(--ok)} .rank .down{color:var(--danger)}
"""
    return layout("Ranking debug", body, "").replace("</style>", extra + "</style>")


def preferences(engines: list[Spec], engines_on: set[str], theme: str, ss: str,
                per: int, q: str = "", new_tab: bool = False,
                deep: str = "auto") -> str:
    def _group(specs: list[Spec]) -> str:
        out = []
        for spec in specs:
            checked = " checked" if spec.name in engines_on else ""
            rec = spec.name in RECOMMENDED
            chip = ' <span class="rec">recommended</span>' if rec else ""
            cls = ' class="is-rec"' if rec else ""
            out.append(f"""<label{cls}><input type="checkbox" name="engines" value="{escape(spec.name)}"{checked}>
      {escape(spec.label)}{chip}<span class="sub">{escape(spec.blurb)}</span></label>""")
        return "".join(out) or '<p class="sub" style="margin:6px 0 0">None available.</p>'

    fast_specs = [s for s in engines if s.tier != "deep"]
    deep_specs = [s for s in engines if s.tier == "deep"]
    fast_tip = ("Indexed web search. Queries a search index and returns links in well under a "
                "second, so these run on every search.")
    deep_tip = ("Agentic retrieval. These read pages before they answer, so a cold call takes "
                "several seconds. They run only when the fast engines come up short, unless you "
                "choose \u201calways\u201d under Search.")

    theme_sel = "".join(
        f'<label><input type="radio" name="theme" value="{v}"'
        f'{" checked" if theme == v else ""}> {lbl}</label>'
        for v, lbl in (("", "System"), ("light", "Light"), ("dark", "Dark")))
    ss_sel = "".join(
        f'<option value="{v}"{" selected" if ss == v else ""}>{v.title()}</option>'
        for v in ("off", "moderate", "strict"))
    per_sel = "".join(
        f'<option value="{n}"{" selected" if per == n else ""}>{n} per page</option>'
        for n in (10, 20, 30))

    body = f"""<header class="top"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form(q)}
  <div class="nav"><a href="/about">About</a></div>
</div></header>
<div class="wrap"><form class="prefs" action="/preferences" method="get">
  <h1>Preferences</h1>
  <fieldset><legend>Engines</legend>
    <div class="egroup"><h2 class="egt">Fast engines
      <span class="help" tabindex="0" role="note" aria-label="What fast engines are: {escape(fast_tip)}">?<span class="tip">{escape(fast_tip)}</span></span>
    </h2>{_group(fast_specs)}
      <p class="reco"><b>Start here:</b> {', '.join(escape(s.label) for s in fast_specs if s.name in RECOMMENDED)}.
        {escape(RECOMMENDED_NOTE)}<span class="caveat">{escape(RECOMMENDED_CAVEAT)}</span></p></div>
    <div class="egroup"><h2 class="egt">Deep engines
      <span class="help" tabindex="0" role="note" aria-label="What deep engines are: {escape(deep_tip)}">?<span class="tip">{escape(deep_tip)}</span></span>
    </h2>{_group(deep_specs)}</div>
    <input type="hidden" name="save" value="1">
    {f'<input type="hidden" name="q" value="{escape(q)}">' if q else ''}
  </fieldset>
  <fieldset><legend>Appearance</legend>
    <label>Theme</label><div style="display:flex;gap:16px">{theme_sel}</div>
  </fieldset>
  <fieldset><legend>Search</legend>
    <label>Safesearch <span class="sub"><select name="ss">{ss_sel}</select></span></label>
    <label>Results <span class="sub"><select name="per">{per_sel}</select></span></label>
    <label><input type="checkbox" name="newtab" value="1"{' checked' if new_tab else ''}>
      Open results in a new tab</label>
    <label>Deep engines <span class="sub"><select name="deep">
      <option value="auto"{' selected' if deep != "always" else ''}>only when needed</option>
      <option value="always"{' selected' if deep == "always" else ''}>always</option>
    </select></span></label>
  </fieldset>
  <button type="submit">Save preferences</button>
</form>
<p style="margin-top:14px;font-size:13px"><a href="/settings/keys">API keys →</a>
  <span class="sub" style="display:inline">encrypted at rest, write-only</span></p>
</div>"""
    extra = """
.egroup{margin:4px 0 10px}
.egt{position:relative;display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:12px 0 2px}
.egroup:first-of-type .egt{margin-top:2px}
/* hover *and* focus, so a keyboard or a finger can open it too */
.help{position:relative;display:inline-flex;align-items:center;justify-content:center;
  width:16px;height:16px;border:1px solid var(--line);border-radius:50%;
  font-size:11px;font-weight:700;line-height:1;color:var(--muted);cursor:help;
  text-transform:none;letter-spacing:0;flex:0 0 auto}
.help:hover,.help:focus{border-color:var(--accent);color:var(--fg);outline:none}
.help .tip{position:absolute;top:150%;left:0;z-index:20;width:min(280px,78vw);
  visibility:hidden;opacity:0;transition:opacity .12s;padding:9px 11px;border-radius:9px;
  background:var(--card);border:1px solid var(--line);box-shadow:0 8px 24px rgba(0,0,0,.18);
  font-size:12px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--fg);
  line-height:1.45;white-space:normal}
.help:hover .tip,.help:focus .tip{visibility:visible;opacity:1}
.rec{font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  color:var(--accent);border:1px solid color-mix(in srgb, var(--accent) 45%, transparent);
  border-radius:99px;padding:1px 6px;margin-left:2px;vertical-align:1px}
.reco{margin:8px 0 2px;padding:8px 11px;border-left:2px solid var(--accent);
  background:color-mix(in srgb, var(--accent) 7%, transparent);border-radius:0 8px 8px 0;
  font-size:12.5px;color:var(--muted);line-height:1.5}
.reco b{color:var(--fg)}
.reco .caveat{display:block;margin-top:4px;opacity:.85;font-size:11.5px}
/* tie the callout to the rows it talks about */
.prefs label.is-rec{background:color-mix(in srgb, var(--accent) 5%, transparent);
  border-radius:7px;padding-left:6px;margin-left:-6px}
/* on a phone the icon sits after the heading text, so a tip pinned to it would run
   off the right edge: drop the anchor to the whole heading and let the tip span it */
@media (max-width:640px){
  .prefs select{max-width:52vw}
  .prefs label{flex-wrap:wrap}
  .help{position:static}
  .help .tip{left:0;right:0;width:auto;max-width:none}
}
"""
    return layout("Preferences · trivet", body, theme).replace("</style>", extra + "</style>")


def keys_page(engines: list[Spec], status: dict[str, dict], can_store: bool,
              csrf: str, message: str = "", theme: str = "") -> str:
    """API keys: write-only in the UI, encrypted at rest by secrets_store."""
    def row(spec: Spec) -> str:
        key_name = spec.env[0] if spec.env else ""
        info = status.get(key_name)
        if info:
            state = (f'<span class="kstate ok">set ···{escape(info["last4"])}</span>'
                     f'<span class="ksub">{_human_age(time.time() - info["updated_at"])} ago</span>')
        else:
            state = '<span class="kstate off">not set</span>'
        disabled = "" if can_store else " disabled"
        remove = (f'<button class="kdel" name="action" value="delete" formnovalidate'
                  f' onclick="return confirm(\'Remove this key? The engine stops working '
                  f'until a new one is saved.\')"{disabled}>Remove</button>'
                  if info else "")
        return f"""<form class="krow" method="post" action="/settings/keys">
      <input type="hidden" name="csrf" value="{escape(csrf)}">
      <input type="hidden" name="key" value="{escape(key_name)}">
      <div class="kname"><b>{escape(spec.label)}</b>
        <span class="ksub">{escape(spec.tier)} · {escape(key_name)}</span></div>
      <div class="kstate-cell">{state}</div>
      <input class="kinput" type="password" name="value" placeholder="paste key"
             autocomplete="off" spellcheck="false" required{disabled}>
      <div class="kacts"><button name="action" value="save"{disabled}>Save</button>{remove}</div>
    </form>"""

    rows = "".join(row(s) for s in engines)
    banner = ""
    if not can_store:
        banner = ('<p class="warn">Storage is disabled: no <code>TRIVET_SECRET_KEY</code> is set. '
                  'Keys can still come from environment variables or a <code>.env</code> file.</p>')
    msg = f'<p class="ok-msg">{escape(message)}</p>' if message else ""
    body = f"""<header class="top"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form("")}
  <div class="nav"><a href="/preferences">Preferences</a><a href="/about">About</a>
    <a href="/logout">Log out</a></div>
</div></header>
<div class="wrap"><div class="prefs" style="max-width:760px">
  <h1>API keys</h1>
  <p style="color:var(--muted);font-size:13px;margin-top:-6px">
  Keys are encrypted with AES-256-GCM before they touch the database, and the page
  only ever shows the last four characters. Leaving a field blank changes nothing.</p>
  {banner}{msg}
  <div class="keys">{rows}</div>
  <p style="color:var(--muted);font-size:12px;margin-top:16px">
  An engine appears in Preferences as soon as its key is saved. Keys set here win over
  a <code>.env</code> file; an exported environment variable wins over both.</p>
</div></div>"""
    extra = """
.keys{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.krow{display:grid;grid-template-columns:1.1fr .9fr 1.4fr auto;gap:10px;align-items:center;
  border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0}
.kname{display:flex;flex-direction:column;gap:2px}
.ksub{color:var(--muted);font-size:11px;font-family:ui-monospace,SFMono-Regular,monospace}
.kstate-cell{display:flex;flex-direction:column;gap:2px}
.kstate{font-size:12.5px;font-variant-numeric:tabular-nums}
.kstate.ok{color:var(--fg)}
.kstate.off{color:var(--muted)}
.kinput{width:100%;min-width:0;border:1px solid var(--line);border-radius:7px;
  background:var(--field);color:var(--fg);padding:7px 9px;font:inherit;font-size:13px}
.kacts{display:flex;gap:6px}
.krow button{border:1px solid var(--line);background:var(--chip);color:var(--fg);
  border-radius:7px;padding:6px 10px;font:inherit;font-size:12.5px;cursor:pointer}
.krow button[value=save]{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.kdel{color:var(--danger);border-color:color-mix(in srgb, var(--danger) 40%, transparent)}
.warn{margin:10px 0 0;padding:9px 11px;border-left:2px solid #b45309;border-radius:0 8px 8px 0;
  background:color-mix(in srgb, #b45309 10%, transparent);font-size:12.5px;color:var(--muted)}
.ok-msg{margin:10px 0 0;padding:9px 11px;border-left:2px solid var(--accent);border-radius:0 8px 8px 0;
  background:color-mix(in srgb, var(--accent) 8%, transparent);font-size:12.5px}
@media (max-width:640px){
  .krow{grid-template-columns:1fr;gap:8px}
  .kacts{justify-content:flex-start}
}
"""
    return layout("API keys · trivet", body, theme).replace("</style>", extra + "</style>")


def about(engines: list[Spec], theme: str = "") -> str:
    rows = "".join(
        f"<tr><td><b>{escape(s.label)}</b></td><td>{escape(s.blurb)}</td>"
        f"<td>{'time filter, safe search' if s.time_range and s.safesearch else 'time filter' if s.time_range else '—'}</td></tr>"
        for s in engines)
    body = f"""<header class="top"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form("")}
  <div class="nav"><a href="/preferences">Preferences</a></div>
</div></header>
<div class="wrap"><div class="prefs">
  <h1>About</h1>
  <p>A search engine for humans. Each query fans out to independent search
  APIs, and the results are fused with Reciprocal Rank Fusion: an ordering built
  from each engine's ranks, not from scores that are not comparable between
  vendors. A result several engines agree on outranks a single engine's top hit.</p>
  <table style="width:100%;border-collapse:collapse;font-size:13.5px">{rows}</table>
  <p style="color:var(--muted);font-size:12.5px;margin-top:18px">
  Duplicate URLs are collapsed before ranking. Syndicated copies of the same
  article are noted under the result, not merged away.</p>
</div></div>"""
    return layout("About · trivet", body, theme)


OPENSEARCH = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>trivet</ShortName>
  <Description>Trivet over agentic search indexes</Description>
  <Url type="text/html" template="{base}/search?q={{searchTerms}}"/>
  <InputEncoding>UTF-8</InputEncoding>
</OpenSearchDescription>"""

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="#1a4fd6"><path d="M4 7H28L26 10H6Z"/><path d="M8 10H13L12 26H7Z"/><path d="M14 10H18V26H14Z"/><path d="M19 10H24L25 26H20Z"/></svg>"""
