"""HTML rendering for the human-facing search UI.

Server-rendered on purpose: the page works with JS off, every result state is a
URL you can bookmark or share, and there is no build step.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlencode, urlsplit

from _version import __version__ as VERSION
from merge import MergedHit
from providers import (BY_NAME, EngineResult, RECOMMENDED, RECOMMENDED_CAVEAT,
                       RECOMMENDED_NOTE, Spec, fmt_news_age)
import thumb_proxy

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
.wrap{max-width:860px;margin:0 auto;padding:0 18px}
form.search{display:flex;align-items:center}
form.search .sb{position:relative;display:flex;align-items:center;flex:1;min-width:0}
form.search input[type=search]{flex:1;background:var(--field);
  border:1px solid color-mix(in srgb,var(--fg) 15%,transparent);
  color:var(--fg);padding:11px 44px 11px 16px;border-radius:24px;font-size:15px;
  outline:none;min-width:0;transition:border-color .12s,box-shadow .12s}
form.search input[type=search]:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 14%,transparent)}
form.search button{position:absolute;right:5px;width:34px;height:34px;padding:0;
  display:inline-flex;align-items:center;justify-content:center;
  background:transparent;border:0;border-radius:50%;color:var(--muted);cursor:pointer}
form.search button:hover,form.search button:focus-visible{color:var(--accent);
  background:color-mix(in srgb,var(--accent) 10%,transparent);outline:none}
form.search .btn-icon{width:18px;height:18px}
form.search .suggestions{position:absolute;top:calc(100% + 7px);left:0;right:0;z-index:80;
  max-height:min(360px,60vh);overflow-y:auto;padding:6px;background:var(--field);
  border:1px solid var(--line);border-radius:14px;box-shadow:0 12px 32px rgba(0,0,0,.18);
  text-align:left;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
form.search .suggestions[hidden]{display:none}
form.search .suggestion{display:flex;align-items:center;min-height:40px;padding:8px 12px;
  border-radius:9px;color:var(--fg);font-size:14px;line-height:1.4;cursor:pointer;
  overflow-wrap:anywhere}
form.search .suggestion:hover,form.search .suggestion.active{
  background:color-mix(in srgb,var(--accent) 11%,transparent)}
form.search .suggestion.active{box-shadow:inset 2px 0 0 var(--accent)}
html[data-theme="dark"] .prefs button{color:#0b0d10}
@media (prefers-color-scheme:dark){
  html:not([data-theme="light"]) .prefs button{color:#0b0d10}
}
.brand{font-size:20px;font-weight:600;letter-spacing:-.02em;color:var(--fg);display:inline-flex;align-items:center;gap:7px;
  font-family:ui-monospace,SFMono-Regular,Cascadia Mono,Roboto Mono,Liberation Mono,DejaVu Sans Mono,monospace}
.brand .mk{width:1.05em;height:1.05em;fill:var(--accent);flex:0 0 auto}
.brand:hover{text-decoration:none}
.brand span{color:var(--accent)}
.home .brand .mk{width:1em;height:1em}
header.top{border-bottom:1px solid var(--line);padding:14px 0}
header.top.has-tabs{padding-bottom:0}
header.top .inner{display:flex;gap:18px;align-items:center}
header.top form.search{flex:1}
header.top .nav{display:flex;gap:14px;font-size:13px;color:var(--muted);white-space:nowrap}
header.top .nav a{color:var(--muted)}
.tabs{display:flex;gap:22px;margin-top:10px;font-size:13.5px}
.tabs a{padding:4px 2px 9px;color:var(--muted);border-bottom:2px solid transparent;
  margin-bottom:-1px;font-weight:500;text-decoration:none}
.tabs a:hover{color:var(--fg);text-decoration:none}
.tabs a.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.home{padding:16vh 0 0;text-align:center}
.home .brand{font-size:38px;display:block;margin-bottom:6px}
.home .tag{color:var(--muted);font-size:14px;margin-bottom:26px}
.home form.search{max-width:560px;margin:0 auto}
.home .engines{margin-top:26px;display:flex;gap:7px;flex-wrap:wrap;
  align-items:center;justify-content:center}
.pill{display:inline-flex;align-items:center;height:22px;padding:0 9px;border-radius:99px;
  font-size:11.5px;font-weight:600;letter-spacing:.01em;white-space:nowrap;
  color:hsl(var(--h) 62% 34%);background:hsl(var(--h) 68% 50% / .13);
  border:1px solid hsl(var(--h) 55% 50% / .22)}
.more{display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:50%;border:1px solid var(--line);
  color:var(--muted);font-size:13px;line-height:1;text-decoration:none;flex:0 0 auto}
.more:hover{border-color:var(--accent);color:var(--accent);text-decoration:none}
@media (prefers-color-scheme:dark){
  html:not([data-theme="light"]) .pill{color:hsl(var(--h) 78% 74%);
    background:hsl(var(--h) 60% 60% / .16);border-color:hsl(var(--h) 60% 60% / .28)}
}
html[data-theme="dark"] .pill{color:hsl(var(--h) 78% 74%);
  background:hsl(var(--h) 60% 60% / .16);border-color:hsl(var(--h) 60% 60% / .28)}
.status{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  color:var(--muted);font-size:12.5px;margin:14px 0 2px;flex-wrap:wrap}
.status .count{color:var(--muted);white-space:nowrap;min-width:0;overflow:hidden;
  text-overflow:ellipsis}
.diag{margin:0;flex:0 0 auto}
.diag summary{display:inline-flex;align-items:center;gap:6px;cursor:pointer;
  color:var(--muted);font-size:12.5px;list-style:none;white-space:nowrap;
  min-height:26px;padding:0 2px;border-radius:6px}
.diag summary:hover .dtxt,.diag summary:hover .plus{color:var(--fg)}
.diag .dtxt{font-size:11.5px;letter-spacing:.01em}
.diag summary::-webkit-details-marker{display:none}
.diag summary:hover{color:var(--fg)}
.diag .plus{display:inline-flex;align-items:center;justify-content:center;
  width:17px;height:17px;border:1px solid color-mix(in srgb,var(--fg) 22%,transparent);
  border-radius:50%;font-size:12px;line-height:1;color:var(--muted)}
.diag summary:hover .plus{border-color:var(--accent)}
.diag[open] .plus{background:var(--chip)}
.diagbody{margin-top:8px;padding:9px 11px;border:1px solid var(--line);border-radius:9px;
  background:color-mix(in srgb,var(--chip) 60%,transparent);max-width:420px}
.diagline{color:var(--muted);font-size:11.5px;margin-bottom:6px}
.tgrid{display:grid;grid-template-columns:1fr auto;gap:3px 14px;font-size:12px;
  font-variant-numeric:tabular-nums}
.trow{display:contents}
.tgrid b{color:var(--fg);font-weight:600}
.tgrid .fail{color:var(--danger)}
.tgrid .skip{color:var(--muted)}
.tnote{grid-column:1 / -1;color:var(--muted);font-size:11px;margin-top:4px}
.diaglinks{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:12px}
.diaglinks a{color:var(--muted)}
.diaglinks a:hover{color:var(--fg)}
/* only the phrases inside the count line are unbreakable; the count line itself
   must wrap, or a 320px viewport overflows by its width */
.meta .count span{white-space:nowrap}
.meta .hit{color:var(--ok)}
.meta .refresh{color:var(--muted);text-decoration:underline;
  text-decoration-color:color-mix(in srgb,var(--muted) 55%,transparent);
  text-underline-offset:2px;font-size:12px}
/* desktop wraps when all 9 web engines are configured so mouse users can reach
   every chip; phones switch to a single swipeable strip below 640px */
.filters{position:relative;display:flex;gap:6px 14px;align-items:center;
  flex-wrap:wrap;margin:8px 0 14px;padding-bottom:11px;
  border-bottom:1px solid var(--line);font-size:13px}
.fgroup{display:flex;align-items:center;gap:5px;flex:0 0 auto}
.fgroup .strip{display:flex;flex-wrap:wrap;gap:3px;align-items:center}
.filters .lbl{color:var(--fg);opacity:.55;margin-right:3px;font-size:11.5px;
  line-height:1;text-transform:uppercase;letter-spacing:.04em}
.filters a{padding:4px 9px;border-radius:14px;color:var(--muted);
  border:1px solid transparent}
.filters a:hover{background:var(--chip);text-decoration:none}
.filters a.on{background:var(--chip);color:var(--fg);font-weight:600;
  border-color:var(--line)}
.filters .sep{width:1px;height:16px;background:var(--line);margin:0 8px}
article.res{padding:17px 0 18px;border-bottom:1px solid var(--line)}
article.res:last-of-type{border-bottom:0}
.quick{border:1px solid color-mix(in srgb,var(--accent) 24%,var(--line));
  background:color-mix(in srgb,var(--accent) 4%,var(--bg));border-radius:12px;
  padding:15px 17px;margin:18px 0 8px;overflow-wrap:anywhere}
.qhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.qhead h2{font-size:11px;line-height:1.2;letter-spacing:.07em;text-transform:uppercase;
  color:var(--accent);margin:0}
.qengine{font-size:12px;color:var(--fg);font-weight:600}
.qsep{color:var(--muted);font-size:12px}
.qmeta{margin-left:auto;font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.qbody{font-size:15px;line-height:1.62}
.qbody p{margin:0 0 10px}.qbody p:last-child{margin-bottom:0}
.qbody a{text-decoration:underline;text-underline-offset:2px}
.qcite{font-size:11px;font-weight:700;line-height:0;margin-left:2px;white-space:nowrap}
.qcluster{white-space:nowrap}
.qcite a{text-decoration:none;padding:0 1px;color:var(--link)}
.qerror{font-size:13px;color:var(--muted);margin:0}
.qsources{margin-top:11px;border-top:1px solid var(--line);padding-top:8px;font-size:12px}
.qsources summary{cursor:pointer;color:var(--muted);min-height:28px;padding:4px 0;font-weight:500}
.qsources summary:hover{color:var(--fg);text-decoration:underline;text-underline-offset:3px}
.qsources ol{margin:6px 0 0;padding-left:22px}
.qsources li{margin:4px 0;padding-left:2px}
.qsources .qdomain{color:var(--muted);margin-left:5px}
.src{display:flex;align-items:baseline;gap:8px;font-size:12px;color:var(--muted);margin-bottom:4px}
.src .outlet{color:var(--fg);font-weight:500;white-space:nowrap;flex:0 0 auto}
.src .dom{color:var(--muted);font-weight:500;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.src .date{margin-left:auto;white-space:nowrap;font-variant-numeric:tabular-nums}
article.res h2{margin:0 0 5px;font-size:20px;font-weight:600;line-height:1.28;letter-spacing:-.005em}
article.res h2 a{color:var(--link)}
article.res p{margin:0;color:var(--fg);opacity:.9;font-size:14.5px;line-height:1.55;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.prov{color:var(--muted);font-size:11.5px;margin-top:5px}
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
.igrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
  gap:16px 14px;margin:12px 0 8px}
.ires{margin:0;min-width:0;display:flex;flex-direction:column;gap:6px}
.ithumb{position:relative;display:block;aspect-ratio:4/3;border-radius:10px;
  overflow:hidden;background:var(--chip);border:1px solid var(--line)}
.ithumb img{width:100%;height:100%;object-fit:cover;display:block;
  transition:transform .16s ease}
.ithumb:hover img{transform:scale(1.02)}
.idim{position:absolute;right:6px;bottom:6px;padding:1px 6px;border-radius:5px;
  background:rgba(15,17,21,.72);color:#f3f5f8;font-size:10.5px;line-height:1.45;
  font-variant-numeric:tabular-nums;pointer-events:none}
.icap{min-width:0;display:flex;flex-direction:column;gap:2px}
.ititle{font-size:13px;font-weight:500;line-height:1.35;color:var(--fg);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ititle:hover{color:var(--link)}
.imeta{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--muted);min-width:0}
.imeta .dom{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.iagree{color:var(--ok);font-weight:600;font-size:11px;flex:0 0 auto}
.ifull{margin-left:auto;flex:0 0 auto;color:var(--muted);font-size:11px;
  padding:1px 6px;border:1px solid var(--line);border-radius:6px;text-decoration:none}
.ifull:hover{color:var(--fg);border-color:var(--accent);text-decoration:none}
.pages{display:flex;gap:10px;justify-content:space-between;align-items:center;
  margin:26px 0 12px;font-size:14px}
.pages a,.pages span{padding:8px 14px;border-radius:8px;border:1px solid var(--line)}
.pages .cur{background:var(--chip);font-weight:600;border-color:transparent}
.pages .nums{display:flex;gap:6px;flex:1;justify-content:center}
.pages .pos{display:none;flex:1;text-align:center;color:var(--muted)}
.pages .side.gap{visibility:hidden}
@media (max-width:480px){
  .pages .nums{display:none}
  .pages .pos{display:block}
  .pages a.side,.pages span.side{min-height:44px;min-width:44px}
}
.pages .side{display:inline-flex;align-items:center;min-height:40px;min-width:40px;
  justify-content:center}
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
  header.top.has-tabs{padding-bottom:0}
  header.top .inner{flex-wrap:wrap;gap:8px 12px;align-items:center}
  .tabs{margin-top:8px;gap:22px}
  .tabs a{padding:6px 2px 9px;min-height:34px;display:inline-flex;align-items:center}
  header.top .brand{order:1;font-size:18px}
  header.top .nav{order:2;margin-left:auto;gap:18px;font-size:13px}
  header.top .nav a{padding:6px 0}
  header.top form.search{order:3;flex:1 1 100%;width:100%}
  form.search input[type=search]{flex:1 1 100%;min-width:0;font-size:16px;
    padding:11px 44px 11px 16px;border-radius:22px}
  header.top form.search button{width:38px;height:38px}
  .status{margin:10px 0 0;font-size:12px;gap:8px;flex-wrap:nowrap}
  .diag summary{font-size:12px;gap:5px}
  /* the word costs the count its space on a phone; the + still opens the panel */
  .diag .dtxt{display:none}
  /* one strip for both filter groups: the container scrolls, so the two groups
     stay on the same line instead of each taking a row of its own */
  .filters{overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;
    -webkit-overflow-scrolling:touch;gap:0;margin:6px 0 12px;padding-bottom:6px}
  .filters::-webkit-scrollbar{display:none}
  .filters::after{content:"";position:sticky;right:0;flex:0 0 32px;align-self:stretch;
    margin-left:-32px;pointer-events:none;
    background:linear-gradient(to right,color-mix(in srgb,var(--bg) 0%,transparent),var(--bg))}
  .fgroup{margin-right:12px;gap:5px}
  .fgroup:last-child{margin-right:0}
  .fgroup .lbl{flex:0 0 auto;margin:0;font-size:10.5px}
  .fgroup .strip{overflow:visible;flex:0 0 auto;flex-wrap:nowrap;gap:4px}
  .filters a{flex:0 0 auto;display:inline-flex;align-items:center;min-height:34px;
    padding:0 11px;font-size:13px}
  article.res{padding:15px 0 16px}
  article.res h2{font-size:18px;line-height:1.3}
  article.res p{font-size:14px;
    overflow:hidden}
  .src{font-size:12px}
  .e{font-size:10px}
  .igrid{grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 10px}
  .ithumb{border-radius:8px}
  .ititle{font-size:12.5px}
  .ifull{padding:2px 7px;min-height:24px;display:inline-flex;align-items:center}
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
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%d %b %Y")
        except ValueError:
            continue
    return text


AUTOSUGGEST_JS = r"""
(() => {
  for (const form of document.querySelectorAll('form.search[data-suggest="1"]')) {
    const input = form.querySelector('input[type="search"]');
    const popup = form.querySelector('.suggestions');
    if (!input || !popup) continue;
    let values = [], active = -1, typed = input.value, timer = 0, ticket = 0, controller = null;

    const cancel = () => {
      clearTimeout(timer);
      ticket++;
      if (controller) controller.abort();
      controller = null;
    };
    const close = () => {
      popup.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    };
    const show = () => {
      const bounds = input.getBoundingClientRect();
      const available = window.innerHeight - bounds.bottom - 23;
      popup.style.maxHeight = `${Math.max(96, Math.min(360, available))}px`;
      popup.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    };
    const setActive = (index) => {
      if (!values.length) return;
      active = (index + values.length) % values.length;
      const options = popup.querySelectorAll('[role="option"]');
      options.forEach((option, i) => {
        const selected = i === active;
        option.classList.toggle('active', selected);
        option.setAttribute('aria-selected', String(selected));
      });
      const current = options[active];
      input.setAttribute('aria-activedescendant', current.id);
      input.value = values[active];
      current.scrollIntoView({block: 'nearest'});
    };
    const choose = (index) => {
      if (index < 0 || index >= values.length) return;
      input.value = values[index];
      typed = input.value;
      cancel();
      close();
      form.requestSubmit();
    };
    const render = (items) => {
      values = items.filter(item => typeof item === 'string').slice(0, 8);
      popup.replaceChildren();
      values.forEach((value, i) => {
        const option = document.createElement('div');
        option.id = `query-suggestion-${i}`;
        option.className = 'suggestion';
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        option.textContent = value;
        option.addEventListener('pointerdown', event => event.preventDefault());
        option.addEventListener('click', () => choose(i));
        popup.append(option);
      });
      if (values.length) show(); else close();
    };
    const load = (activate = 0) => {
      const query = input.value.trim();
      cancel();
      const mine = ticket;
      controller = new AbortController();
      fetch('/api/suggest?q=' + encodeURIComponent(query), {
        headers: {'Accept': 'application/json'},
        signal: controller.signal,
      }).then(response => {
        if (!response.ok || !(response.headers.get('content-type') || '').includes('application/json')) {
          throw new Error('suggestions unavailable');
        }
        return response.json();
      }).then(data => {
        if (mine !== ticket || document.activeElement !== input || input.value.trim() !== query) return;
        render(Array.isArray(data.suggestions) ? data.suggestions : []);
        if (activate && values.length) setActive(activate > 0 ? 0 : values.length - 1);
      }).catch(error => {
        if (error.name === 'AbortError' || mine !== ticket) return;
        values = [];
        close();
      });
    };

    input.addEventListener('input', () => {
      typed = input.value;
      values = [];
      close();
      cancel();
      const query = input.value.trim();
      if (query.length < 2 || query.startsWith('!')) return;
      timer = setTimeout(() => load(), 220);
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const down = event.key === 'ArrowDown';
        if (!values.length) {
          typed = input.value;
          if (input.value.trim().length >= 2) load(down ? 1 : -1);
        } else {
          if (popup.hidden) show();
          setActive(active < 0 ? (down ? 0 : values.length - 1) : active + (down ? 1 : -1));
        }
      } else if (event.key === 'Enter' && !popup.hidden && active >= 0) {
        event.preventDefault();
        choose(active);
      } else if (event.key === 'Escape' && !popup.hidden) {
        event.preventDefault();
        event.stopPropagation();
        input.value = typed;
        values = [];
        cancel();
        close();
      } else if (event.key === 'Tab') {
        values = [];
        cancel();
        close();
      }
    });
    form.addEventListener('submit', () => { cancel(); close(); });
    form.addEventListener('focusout', event => {
      if (!form.contains(event.relatedTarget)) { values = []; cancel(); close(); }
    });
    document.addEventListener('pointerdown', event => {
      if (!form.contains(event.target)) { values = []; cancel(); close(); }
    });
  }
})();
"""


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
<script>{AUTOSUGGEST_JS}</script>
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


# One hue per engine so the same name is always the same colour, in every theme.
ENGINE_HUES = {
    "keenable": 212, "exa": 268, "brave": 24, "tavily": 158,
    "serper": 44, "linkup": 190, "firecrawl": 12, "valyu": 296,
    "parallel": 232,
}


def _engine_pill(spec: Spec) -> str:
    hue = ENGINE_HUES.get(spec.name, 215)
    return (f'<span class="pill" style="--h:{hue}" title="{escape(spec.blurb)}">'
            f'{escape(spec.label)}</span>')


def _search_form(q: str, mode: str = "web", suggest: bool = False) -> str:
    # The submit stays a real button so Enter and keyboard users work with JS off;
    # autocomplete only enhances that form when both the preference and Serper exist.
    tab_input = (f'<input type="hidden" name="tab" value="{escape(mode)}">'
                 if mode in ("images", "news") else "")
    combo_attrs = (' role="combobox" aria-autocomplete="list" aria-expanded="false"'
                   ' aria-controls="query-suggestions"' if suggest else "")
    popup = ('<div id="query-suggestions" class="suggestions" role="listbox"'
             ' aria-label="Search suggestions" hidden></div>' if suggest else "")
    enabled = ' data-suggest="1"' if suggest else ""
    return f"""<form class="search" action="/search" method="get" role="search"{enabled}>{tab_input}
  <div class="sb">
    <input type="search" name="q" value="{escape(q)}" placeholder="Search, or !bang a site"
           autocomplete="off" spellcheck="false" aria-label="Search query"{combo_attrs}>
    <button type="submit" aria-label="Search">
      <svg class="btn-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"
        fill="none" stroke="currentColor" stroke-width="2.2"/><line x1="16" y1="16" x2="21" y2="21"
        stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>
    </button>
    {popup}
  </div>
</form>"""


def _tabs(q: str, time_range: str, ss: str, mode: str = "web") -> str:
    """Primary Web / Images mode switcher.

    Switching tabs intentionally drops per-tab `engines` and `per` overrides so
    moving from Images back to Web restores the user's full web engine set.
    """
    def href(target: str) -> str:
        params: dict[str, str] = {"q": q}
        if target in ("images", "news"):
            params["tab"] = target
        if time_range and time_range != "any":
            params["time"] = time_range
        if ss and ss != "moderate":
            params["ss"] = ss
        return "/search?" + urlencode(params)

    def link(key: str, label: str) -> str:
        cur = ' class="on" aria-current="page"' if mode == key else ""
        return f'<a{cur} href="{escape(href(key))}">{label}</a>'

    return (f'<nav class="tabs" aria-label="Search type">'
            f'{link("web", "Web")}{link("images", "Images")}{link("news", "News")}'
            f'</nav>')


def home(enabled: list[Spec], configured: list[Spec], theme: str = "",
         suggest: bool = False) -> str:
    off = len(configured) - len(enabled)
    pills = " ".join(_engine_pill(s) for s in enabled)
    more = (f'<a class="more" href="/preferences" title="{off} more available"'
            f' aria-label="{off} more engines available">+</a>' if off > 0 else "")
    engines_block = (f'<span class="pills">{pills}</span>{more}' if enabled else
                     '<a href="/settings/keys">No engines yet. Add an API key</a>')
    body = f"""<div class="wrap"><div class="home">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  <div class="tag">A search engine over agentic indexes.</div>
  {_search_form("", suggest=suggest)}
  <div class="engines">{engines_block}</div>
  <footer style="justify-content:center;border:0;margin-top:26px">
    <a href="/preferences">Preferences</a><a href="/stats">Engine stats</a>
    <a href="/about">About</a>
  </footer>
</div></div>"""
    return layout("trivet", body, theme)


def _filters(q: str, engines_on: set[str], all_engines: list[Spec],
             time_range: str, ss: str, per: int, mode: str = "web") -> str:
    def base(**over):
        params: dict[str, object] = {
            "q": q,
            "tab": mode if mode in ("images", "news") else "",
            "engines": ",".join(sorted(engines_on)),
            "time": time_range,
            "ss": ss,
            "per": per,
        }
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
            rep=None, new_tab: bool = False, mode: str = "web") -> str:
    if mode == "news":
        date = escape(fmt_news_age(hit.published)) if hit.published else ""
    else:
        date = f' · {escape(_fmt_date(hit.published))}' if hit.published else ""
    if hit.source_name and hit.source_name.lower() != hit.domain.lower():
        src_lead = (f'<span class="outlet">{escape(hit.source_name)}</span>'
                    f'<span class="dom">{escape(hit.domain)}</span>')
    else:
        src_lead = f'<span class="dom">{escape(hit.domain)}</span>'
    # stable badge order (Spec order), so the same page always renders the same way
    order = {s.name: i for i, s in enumerate(engines.values())}
    names = [engines[e].label for e in sorted(hit.engines, key=lambda n: order.get(n, 99))
             if e in engines]
    provenance = (f'<div class="prov">Found by {escape(" · ".join(names))}</div>'
                  if names else "")
    # Same page, different query string: that is a URL variant, not a reprint, and
    # saying otherwise reads as a broken duplicate to a human.
    dupe = ""
    if len(hit.duplicates) >= 2:
        dupe = f'<div class="dupes">{len(hit.duplicates) + 1} URLs merged</div>'
    return f"""<article class="res">
  <div class="src">{src_lead}{_flag(rep)}<span class="date">{date}</span></div>
  <h2><a href="{escape(_safe_href(hit.url))}" rel="noopener"{' target="_blank"' if new_tab else ''} data-pos="{position}">{escape(hit.title)}</a></h2>
  {f'<p>{escape(hit.snippet)}</p>' if hit.snippet else ''}
  {provenance}
  {dupe}
</article>"""


def _image_result(hit: MergedHit, engines: dict[str, Spec],
                  rep=None, new_tab: bool = False) -> str:
    src = thumb_proxy.proxy_path(hit.thumb_url or hit.image_url)
    if not src:
        return ""
    order = {s.name: i for i, s in enumerate(engines.values())}
    names = [engines[e].label for e in sorted(hit.engines, key=lambda n: order.get(n, 99))
             if e in engines]
    prov = "Found by " + " · ".join(names) if names else ""
    agree = (f'<span class="iagree" title="{escape(prov)}">{len(names)}×</span>'
             if len(names) > 1 else "")
    dim = (f'<span class="idim">{hit.width}×{hit.height}</span>'
           if hit.width and hit.height else "")
    page_href = _safe_href(hit.url)
    full_href = _safe_href(hit.image_url or hit.url)
    target = ' target="_blank"' if new_tab else ""
    prov_attr = f' title="{escape(prov)}"' if prov else ""
    return f"""<figure class="ires"{prov_attr}>
  <a class="ithumb" href="{escape(page_href)}" rel="noopener"{target}>
    <img src="{escape(src)}" alt="{escape(hit.title)}" loading="lazy" decoding="async">
    {dim}
  </a>
  <figcaption class="icap">
    <a class="ititle" href="{escape(page_href)}" rel="noopener"{target}>{escape(hit.title)}</a>
    <div class="imeta">
      <span class="dom">{escape(hit.domain)}</span>{_flag(rep)}{agree}
      <a class="ifull" href="{escape(full_href)}" target="_blank" rel="noopener"
         aria-label="Open full image from {escape(hit.domain)}">Full</a>
    </div>
  </figcaption>
</figure>"""


_ANSWER_TOKEN = re.compile(
    r"\[([^\]\n]{1,300})\]\((https?://[^\s)]+)\)|((?:\[\d{1,2}\])+)|(\*\*[^*\n]{1,300}\*\*)")


def _answer_inline(text: str, citations: list, target: str) -> str:
    """Escape vendor prose while preserving only links, [n] citations and bold."""
    out: list[str] = []
    pos = 0
    for match in _ANSWER_TOKEN.finditer(text):
        out.append(escape(text[pos:match.start()]).replace("\n", "<br>"))
        label, url, number, bold = match.groups()
        if url:
            safe = _safe_href(url)
            out.append(f'<a href="{escape(safe)}" rel="noopener"{target}>{escape(label)}</a>')
        elif number:
            cluster: list[str] = []
            for marker in re.findall(r"\d{1,2}", number):
                index = int(marker) - 1
                if 0 <= index < len(citations) and _safe_href(citations[index].url) != "#":
                    source = citations[index]
                    cluster.append(
                        f'<sup class="qcite"><a href="{escape(_safe_href(source.url))}" '
                        f'rel="noopener"{target} aria-label="Source {marker}: {escape(source.title)}">'
                        f'[{marker}]</a></sup>')
                else:
                    cluster.append(escape(f"[{marker}]"))
            out.append(f'<span class="qcluster">{"".join(cluster)}</span>')
        elif bold:
            out.append(f"<strong>{escape(bold[2:-2])}</strong>")
        pos = match.end()
    out.append(escape(text[pos:]).replace("\n", "<br>"))
    return "".join(out)


def _quick_answer(answer, new_tab: bool = False) -> str:
    if answer is None:
        return ""
    spec = BY_NAME.get(answer.engine)
    label = spec.label if spec else answer.engine
    if answer.cached_age is not None:
        age = "just now" if answer.cached_age < 10 else f"{_human_age(answer.cached_age)} ago"
        timing = f"cached {age}"
        timing_title = "Cached answer age"
    else:
        timing = f"{answer.ms / 1000:.1f}s"
        timing_title = "Answer provider latency"
    target = ' target="_blank"' if new_tab else ""
    head = (f'<div class="qhead"><h2 id="quick-answer-title">Quick answer</h2>'
            f'<span class="qsep" aria-hidden="true">·</span>'
            f'<span class="qengine">{escape(label)}</span>'
            f'<span class="qmeta" title="{timing_title}">{escape(timing)}</span></div>')
    if not answer.ok:
        return (f'<aside class="quick" aria-labelledby="quick-answer-title">{head}'
                f'<p class="qerror">{escape(label)} could not answer '
                f'({escape(answer.error or "unknown error")}). Web results are unaffected.</p></aside>')
    paragraphs = re.split(r"\n\s*\n", answer.text.strip())
    content = "".join(f'<p>{_answer_inline(p, answer.citations, target)}</p>'
                      for p in paragraphs if p.strip())
    sources = ""
    visible_sources = []
    seen_urls = set()
    for citation in answer.citations:
        if _safe_href(citation.url) == "#" or citation.url in seen_urls:
            continue
        seen_urls.add(citation.url)
        visible_sources.append(citation)
    if visible_sources:
        rows = "".join(
            f'<li><a href="{escape(_safe_href(c.url))}" rel="noopener"{target}>'
            f'{escape(c.title)}</a><span class="qdomain">{escape(urlsplit(c.url).netloc)}</span></li>'
            for c in visible_sources)
        sources = (f'<details class="qsources"><summary>{len(visible_sources)} '
                   f'{"source" if len(visible_sources) == 1 else "sources"}</summary>'
                   f'<ol>{rows}</ol></details>')
    return (f'<aside class="quick" aria-labelledby="quick-answer-title">{head}'
            f'<div class="qbody">{content}</div>{sources}</aside>')


def results_page(*, q: str, hits: list[MergedHit], page: int, per: int,
                 total_unique: int, elapsed: float, results: list[EngineResult],
                 engines_on: set[str], all_engines: list[Spec], time_range: str,
                 ss: str, theme: str = "", error: str = "",
                 cached_age: float | None = None,
                 impression_id: str = "",
                 reps: dict | None = None,
                 new_tab: bool = False,
                 mode: str = "web", suggest: bool = False,
                 quick_answer=None) -> str:
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
    if mode == "images":
        unit = "image" if total_unique == 1 else "images"
    elif mode == "news":
        unit = "article" if total_unique == 1 else "articles"
    else:
        unit = "result" if total_unique == 1 else "results"
    count_plain = f"{total_unique:,} {unit}"
    answer_hint = answered

    pager_bits = []
    if page > 1:
        pager_bits.append(f"page {page}")
    pager_bits.append(
        f'<a class="refresh" href="'
        f'{escape(_page_url(q, page, engines_on, time_range, ss, per, mode) + chr(38) + "nocache=1")}" '
        f'title="Search the engines again instead of using the cache">↻ refresh</a>')
    pager_html = f'<span class="pager">{" · ".join(pager_bits)}</span>'

    reps = reps or {}
    if mode == "images":
        tiles = "".join(_image_result(h, by_name, reps.get(h.domain), new_tab)
                        for h in hits)
        body_hits = f'<div class="igrid">{tiles}</div>' if tiles else ""
    else:
        body_hits = "".join(_result(h, by_name, (page - 1) * per + i, reps.get(h.domain),
                                    new_tab, mode=mode)
                            for i, h in enumerate(hits, start=1))
    if not body_hits:
        if mode in ("images", "news") and not all_engines:
            body_hits = (f'<div class="none">No {mode}-capable engines are configured. '
                         f'<a href="/settings/keys">Add an API key</a>.</div>')
        else:
            kind = {"images": "images", "news": "news articles"}.get(mode, "results")
            body_hits = (f'<div class="none">No {kind} for <b>{escape(q)}</b>. '
                         f'Try fewer words or a wider time range.</div>')

    # pagination
    # three parts: previous, the number strip, next. Phones drop the strip and keep
    # the position plus Next, which is the only control you actually press there.
    prev_link = next_link = ""
    nums: list[str] = []
    if hits or page > 1:
        if page > 1:
            prev_link = (f'<a class="side" rel="prev" href="'
                         f'{escape(_page_url(q, page - 1, engines_on, time_range, ss, per, mode))}">← Previous</a>')
        for p in range(max(1, page - 2), page + 3):
            if p == page:
                nums.append(f'<span class="cur">{p}</span>')
            elif p == 1 or p <= page + 2:
                nums.append(f'<a href="{escape(_page_url(q, p, engines_on, time_range, ss, per, mode))}">{p}</a>')
        if len(hits) == per:
            next_link = (f'<a class="side" rel="next" href="'
                         f'{escape(_page_url(q, page + 1, engines_on, time_range, ss, per, mode))}">Next →</a>')
    pages = ([prev_link or '<span class="side gap"></span>'],
             [f'<span class="nums">{"".join(nums)}</span>',
              f'<span class="pos">page {page}</span>'],
             [next_link or '<span class="side gap"></span>'])

    body = f"""<header class="top has-tabs"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form(q, mode, suggest=suggest)}
  <div class="nav"><a href="/preferences">Preferences</a><a href="/about">About</a><a href="/logout">Log out</a></div>
</div><div class="wrap">{_tabs(q, time_range, ss, mode)}</div></header>
<div class="wrap">
  {_quick_answer(quick_answer, new_tab)}
  <div class="status">
    <span class="count">{count_plain}</span>
    <details class="diag">
      <summary aria-label="Search details: {answer_hint}">{timing}<span class="dtxt">details</span><span class="plus" aria-hidden="true">+</span></summary>
      <div class="diagbody">
        <div class="diagline">{answered}</div>
        {_timings_grid(results, cached_age, {s.name: s.label for s in all_engines})}
        <div class="diaglinks">{pager_html}
          {_report_link(q, hits, page=page, per=per, results=results, engines_on=engines_on,
                        time_range=time_range, ss=ss, cached_age=cached_age,
                        labels={s.name: s.label for s in all_engines}, mode=mode)}</div>
      </div>
    </details>
  </div>
  {fail_box}
  {_filters(q, engines_on, all_engines, time_range, ss, per, mode)}
  {body_hits}
  <nav class="pages" aria-label="Result pages">{''.join(pages[0] + pages[1] + pages[2])}</nav>
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


def _page_url(q, page, engines_on, time_range, ss, per, mode: str = "web") -> str:
    params: dict[str, object] = {
        "q": q,
        "tab": mode if mode in ("images", "news") else "",
        "page": page,
        "engines": ",".join(sorted(engines_on)),
        "time": time_range,
        "ss": ss,
        "per": per,
    }
    return "/search?" + urlencode({k: v for k, v in params.items() if v})


def login(next_path: str = "/", error: str = "") -> str:
    err = (f'<p class="err">{escape(error)}</p>' if error else "")
    body = f"""<div class="wrap"><div class="home" style="padding-top:14vh">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  <div class="tag">Sign in to search.</div>
  <form class="search login" method="post" action="/login" style="max-width:420px;margin:0 auto">
    <input type="hidden" name="next" value="{escape(next_path)}">
    <div class="sb">
      <input type="password" name="password" placeholder="Password" autofocus
             autocomplete="current-password" aria-label="Password" required>
    </div>
    <button type="submit" class="solid">Sign in</button>
  </form>
  {err}
  <p class="hint">Stays signed in on this browser for 90 days.</p>
</div></div>"""
    extra = """
.login .sb{flex:1}
.login input{background:var(--field);border:1px solid color-mix(in srgb,var(--fg) 15%,transparent);
  color:var(--fg);padding:11px 16px;border-radius:24px;font-size:16px;min-width:0;outline:none}
.login input:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 14%,transparent)}
.login button.solid{position:static;width:auto;height:auto;flex:0 0 auto;white-space:nowrap;
  padding:11px 20px;border-radius:24px;background:var(--accent);color:#fff;
  font-size:14px;font-weight:600;display:inline-flex;align-items:center;justify-content:center}
.login button.solid:hover{background:var(--accent);color:#fff;opacity:.92}
html[data-theme="dark"] .login button.solid{color:#0b0d10}
@media (prefers-color-scheme:dark){
  html:not([data-theme="light"]) .login button.solid{color:#0b0d10}
}
.home .err{color:var(--danger);font-size:13px;margin-top:14px}
.home .hint{color:var(--muted);font-size:12px;margin-top:22px}
"""
    return layout("Sign in · trivet", body, "").replace("</style>", extra + "</style>")


# --- "report a problem" ------------------------------------------------------
# A fork should point at its own tracker rather than upstream.
REPO = os.environ.get("TRIVET_REPO", "jcrabapple/trivet")
ISSUE_URL_LIMIT = 7000        # GitHub gets unhappy well before a URL gets long


def _report_json(hits: list, limit: int) -> str:
    rows = []
    for i, h in enumerate(hits[:limit], start=1):
        rows.append({
            "pos": i,
            "title": (h.title or "")[:120],
            "url": (h.url or "")[:220],
            "engines": list(h.engines or []),
            "score": round(float(getattr(h, "score", 0.0) or 0.0), 3),
        })
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _issue_url(q: str, hits: list, *, page: int, per: int, results: list,
               engines_on: set[str], time_range: str, ss: str,
               cached_age: float | None = None, repo: str | None = None,
               labels: dict[str, str] | None = None,
               mode: str = "web") -> str:
    """Prefilled GitHub issue: the query plus a JSON dump of what was shown.

    A URL has a practical length limit, so the dump shrinks (10 → 6 → 3 → 1 results)
    until it fits, and says so when it was cut. The query always survives.
    """
    repo = repo or REPO
    base = f"https://github.com/{repo}/issues/new"
    prefix = {"images": "Image search problem",
              "news": "News search problem"}.get(mode, "Search results problem")
    title = f"{prefix}: {q[:110]}"
    label = labels or {}
    engines = ", ".join(sorted(label.get(e, e) for e in engines_on)) or "none"
    timings = " · ".join(
        f"{label.get(r.name, r.name)}{'' if r.ok else ' (failed)'} {r.ms}ms"
        for r in results) or "n/a"
    mode_tag = f"{mode}, " if mode != "web" else ""
    view = (f"{mode_tag}page {page}, {per} per page, {time_range}, safesearch {ss}"
            + (f", served from cache ({int(cached_age)}s old)" if cached_age else ""))
    header = (
        f"**Query:** `{q}`\n\n"
        f"**When:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"**Trivet:** {VERSION}\n"
        f"**Engines:** {engines}\n"
        f"**Timings:** {timings}\n"
        f"**View:** {view}\n\n"
        "### What went wrong?\n\n"
        "<!-- e.g. \"the answer is the 4th result\", \"these are all SEO spam\", "
        "\"this is the wrong version\" -->\n")
    for limit in (10, 6, 3, 1):
        cut = f"\n_(cut to {limit} of {len(hits)} results to fit in a URL)_" if len(hits) > limit else ""
        body = f"{header}\n### Results\n\n```json\n{_report_json(hits, limit)}\n```\n{cut}\n"
        url = f"{base}?{urlencode({'title': title, 'body': body})}"
        if len(url) <= ISSUE_URL_LIMIT:
            return url
    # even one result did not fit: keep the context, drop the dump
    body = header + "\n### Results\n\n_(too long to fit in a link - paste them here)_\n"
    return f"{base}?{urlencode({'title': title, 'body': body})}"


def _report_link(q: str, hits: list, **kw) -> str:
    url = _issue_url(q, hits, **kw)
    return (f'<a class="report" href="{escape(url)}" target="_blank" rel="noopener">'
            f'Report a problem with these results</a>')


def _timings_grid(results: list, cached_age: float | None,
                  labels: dict[str, str] | None = None) -> str:
    """Per-engine latency, shown inside the status disclosure rather than as a strip."""
    if not results:
        return ""
    rows = []
    for r in results:
        label = (labels or {}).get(r.name, r.name)
        if getattr(r, "skipped", False):
            value = '<span class="skip">skipped</span>'
        elif r.ok:
            ms = r.ms
            value = f'<b>{ms}ms</b>' if ms < 1000 else f'<b>{ms / 1000:.1f}s</b>'
        else:
            value = f'<span class="fail">{escape(r.error or "failed")}</span>'
        rows.append(f'<div class="trow"><span>{escape(label)}</span>{value}</div>')
    note = "timings from the original fetch" if cached_age is not None else ""
    return ('<div class="tgrid">' + "".join(rows) +
            (f'<div class="tnote">{note}</div>' if note else "") + "</div>")


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


def stats_page(rows: list[dict], hours: int, labels: dict[str, str] | None = None,
               suggest: bool = False) -> str:
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
  {_search_form("", suggest=suggest)}
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
.report{margin:0 0 12px;font-size:11.5px}
.report a{color:var(--muted)}
.report a:hover{color:var(--fg)}
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
                deep: str = "auto", serper_available: bool = False,
                suggest: bool = False, answer_engines: list[Spec] | None = None,
                answer: bool = False, answer_engine: str = "") -> str:
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
    answer_engines = answer_engines or []
    answer_sel = "".join(
        f'<option value="{escape(s.name)}"{" selected" if answer_engine == s.name else ""}>'
        f'{escape(s.label)}</option>' for s in answer_engines)
    if not answer_engines:
        answer_sel = '<option value="">No supported API key</option>'

    body = f"""<header class="top"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form(q, suggest=suggest and serper_available)}
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
    <label class="suggest-pref"><input type="checkbox" name="suggest" value="1"{' checked' if suggest and serper_available else ''}{'' if serper_available else ' disabled'}>
      Live search suggestions <span class="sub">{'via Google (Serper)' if serper_available else 'requires the Serper API key'}</span></label>
    <label class="suggest-pref"><input type="checkbox" name="answer" value="1"{' checked' if answer and answer_engines else ''}{'' if answer_engines else ' disabled'}>
      Quick answer <span class="sub">{'end a query with ?' if answer_engines else 'requires a supported API key'}</span></label>
    <label>Answer engine <span class="sub"><select name="answer_engine"{'' if answer_engines else ' disabled'}>{answer_sel}</select></span></label>
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
.prefs .suggest-pref{flex-wrap:wrap}
.prefs .suggest-pref .sub{flex:1 1 auto;text-align:right}
@media (max-width:480px){
  .prefs .suggest-pref .sub{flex-basis:100%;margin-left:22px;text-align:left}
}
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
              csrf: str, message: str = "", theme: str = "",
              suggest: bool = False) -> str:
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
  {_search_form("", suggest=suggest)}
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


def about(engines: list[Spec], theme: str = "", suggest: bool = False) -> str:
    def caps(s: Spec) -> str:
        bits = []
        if s.image_fn is not None:
            bits.append("images")
        if s.news_fn is not None:
            bits.append("news")
        if s.time_range:
            bits.append("time filter")
        if s.safesearch:
            bits.append("safe search")
        return ", ".join(bits) if bits else "—"

    rows = "".join(
        f"<tr><td><b>{escape(s.label)}</b></td><td>{escape(s.blurb)}</td>"
        f"<td>{escape(caps(s))}</td></tr>"
        for s in engines)
    body = f"""<header class="top"><div class="wrap inner">
  <a class="brand" href="/"><svg class="mk" viewBox="0 0 512 512" aria-hidden="true"><path d="M96 112H416L384 160H128Z"/><path d="M128 160H176L164 400H120Z"/><path d="M232 160H280L276 400H236Z"/><path d="M336 160H384L392 400H348Z"/></svg>trivet</a>
  {_search_form("", suggest=suggest)}
  <div class="nav"><a href="/preferences">Preferences</a></div>
</div></header>
<div class="wrap"><div class="prefs">
  <h1>About <span class="sub" style="font-size:12px">{escape(VERSION)}</span></h1>
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
