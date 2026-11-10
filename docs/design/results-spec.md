# Trivet results redesign

## Recommendation: A — Calm list

A best preserves fused ranking while making the answer primary. Its 20px titles (18px on phones) own the hierarchy; domain/date and plain-text provenance recede. The single horizontally scrollable filter row and one diagnostics disclosure replace three equal-weight chrome lines, putting the first title at 254px at both phone test widths. Two-line snippets establish a consistent scanning rhythm. Engine names remain available without badge wrapping, and pagination has 48px coarse-pointer targets. B is a strong reading-list alternative, but its rail narrows long titles; C exposes diagnostics well but intentionally sacrifices snippet context and title prominence.

## Implementation deltas

| Selector / structure | Current | Recommended A |
|---|---|---|
| `.meta`, `.timings`, `.report` | Three separate rows | One `.status` row: count plus native `<details>` containing the timing grid, refresh, and report link |
| `.filters` | Two wrapping groups, bottom margin 18px | `display:flex; flex-wrap:nowrap; overflow-x:auto; gap:11px; margin:0 0 5px; padding:4px 0 9px`; keep both groups in one strip |
| `.filters a` | 4px 10px padding | 32px minimum height desktop; 44px with 12px inline padding under `pointer:coarse` |
| `.src` | Domain, date, badge container together | Domain and date only; `display:flex; margin-bottom:5px`; date aligned end |
| `.src .engines` | Badges in source row | Move engine names to `.provenance` after the snippet; plain 11.5px muted text, no pills |
| `article.res` | 14px 0 16px | `padding:18px 0 19px`; retain one hairline separator |
| `article.res h2` | 18px/1.35; 17px phone | `600–650` weight, `20px/1.28`; `18px/1.3` at ≤640px |
| `article.res .url` | Full URL row | Omit visible URL; the destination remains on the title link |
| `article.res p` | Unclamped desktop, three lines phone | `font-size:14.5px; line-height:1.55`; two-line clamp everywhere (14px phone) |
| `.pages` | Centered number row | Three-part grid: previous, current/numbers, next; below 480px show `1 / 3` and Next only |
| `.wrap` | Maximum 760px | `max-width:860px`; 16px gutters, 12px at ≤380px |

Use the existing theme variables unchanged, including both dark selectors. Native disclosures require no scripting; overflow rows get a 26px right-edge fade. Keep focus-visible outlines and 16px mobile search input.

## Responsive behavior

- **320px:** 296px content width. Header uses brand/nav then search. Status stays one line; filters and their off-screen options scroll horizontally. Source dates remain on one line, snippets clamp to two lines, and pagination reduces to current position plus a 118px Next target. Diagnostics panel fits within 296px.
- **390px:** Same composition with 358px content. Approximately three full results are visible before the fourth begins; no result metadata wraps into badges.
- **1280px:** Content caps at 860px. Search joins brand/nav in one header row; title size rises to 20px. Filters remain one strip rather than stretching into multiple rows.

## Text contrast (light / dark)

No new palette colors are introduced.

| Foreground / background | Ratio light | Ratio dark |
|---|---:|---:|
| `fg` / `bg` | 16.88:1 | 14.77:1 |
| `muted` / `bg` | 4.83:1 | 7.05:1 |
| `link` / `bg` | 6.70:1 | 8.51:1 |
| `visited` / `bg` | 6.99:1 | 8.63:1 |
| `fg` / `chip` (active filters) | 15.45:1 | 12.96:1 |
| `fg` / `field` | 16.88:1 | 13.42:1 |
| `muted` / `field` | 4.83:1 | 6.41:1 |
| `link` / `field` | 6.70:1 | 7.74:1 |
| `visited` / `field` | 6.99:1 | 7.84:1 |

## Deliberately unchanged

Result order, titles, destinations, snippets, dates, domains, engine membership, fusion/footer facts, search behavior, filter URLs, refresh behavior, prefilled report destination, pagination destinations, theme tokens, and the existing click-attribution integration contract are unchanged. No safe-search controls, thumbnails, cards, icons per engine, new ranking labels, or inferred content were added.
