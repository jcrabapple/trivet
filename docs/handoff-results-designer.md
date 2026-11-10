# Handoff: redesign the Trivet results page

You are redesigning a real page. Mockups and a spec, no application code.

## What it is

`~/projects/trivet` — an open-source metasearch engine, server-rendered HTML, no build step,
no JavaScript required. A query fans out to several paid search APIs, results are fused by
rank and re-ranked locally. The user of this page is technical, self-hosting, and reads it on
a phone as often as a desktop.

**Study the real thing first:** `docs/design/current-results.html` is a captured response for
the query "best android browser" with ten results — real markup, real CSS, both themes
defined as custom properties at the top of the `<style>` block. Open it, read it, and treat it
as the baseline you are improving.

## Hard constraints

- **No JavaScript.** The page must work with scripting disabled. A `<details>` element is fine;
  a script that expands filters is not.
- **No external assets.** No web fonts, no icon CDNs, no images. Inline SVG only. The app's
  CSP forbids everything but inline styles/scripts and same-origin images.
- **No new data.** Everything on the page today is available; you may restructure, group,
  de-emphasise or hide-on-small-screens, but not invent fields the app could not supply.
- Must be legible and operable from **320px to 1280px**, in light and dark themes, with
  `prefers-color-scheme` and an explicit `data-theme` override both working.
- Keep these, in some form: the query's result count, the per-engine timings strip, the
  "report a bad result set" link, the time and engine filters, the pagination.

## What is wrong with it now (your brief, but verify by looking)

1. **Too much before the first result on a phone.** Meta line, timings line, report link,
   two filter strips — the answer starts below the fold.
2. **Three things fight on one line per result**: domain, date, and one badge per engine that
   returned it. On mobile the badges wrap and the title drops.
3. **Snippets** are three clamped lines of machine-cleaned text; at 10 results the page is long
   and the eye has no rhythm to scan with.
4. **The top strip reads as noise**: `About 30 results · 1.97s · all 4 engines answered`,
   then `Keenable 324ms · Exa 1.4s · ...`, then a report link. Three lines of chrome, all
   similar weight.
5. **Pagination** at the bottom is a row of numbers that is fiddly on a phone.

## Deliverables — write these files

1. `docs/design/results-a.html`, `results-b.html`, `results-c.html` — three **distinct**
   directions, each a self-contained static page (inline `<style>`, the same sample data,
   both themes wired to the same custom properties the current page uses). They must open and
   render on their own, offline.
2. `docs/design/results-spec.md` — under 700 words:
   - which direction you recommend and why, in terms of the five problems above
   - the concrete CSS/structure deltas to implement it: selector, current rule, new rule
   - what happens at 320px, 390px and 1280px
   - contrast of every new colour pair, with the ratio
   - what you deliberately did NOT change

## Direction prompts (all three, not one concept in three colours)

- **A. Calm list.** Typographic hierarchy does the work: result title is the loudest thing on
  the page, everything else recedes. Filters become a single compact row. Chrome shrinks.
- **B. Grouped by source.** A result's engine badges become a quiet left rail or a leading
  marker, so scanning down the page is by domain first, then title. Think of a reading list,
  not a SERP.
- **C. Dense console.** More results visible per screen, monospace accents, timings and engine
  data promoted (this is a tool for someone who wants to know which engine found what). Must
  still be readable at 320px, which is the hard part.

## Method

Read the current page. Look at the actual CSS variables and reuse them so the mockups are
plausible rather than restyled from scratch. Build all three, then write the spec. If you run
short on budget, the order that matters is: `results-a.html`, then the spec, then b and c.

Write files, then stop. No chat summary.
