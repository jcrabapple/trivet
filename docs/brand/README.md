# Trivet brand

Three independent supports carry one surface: a stable search surface built out of
indexes that do not agree with each other. That is the mark.

## Files

| File | Use |
|---|---|
| `icon.svg` | primary mark, light-theme accent `#1a4fd6`, 512 grid |
| `icon-dark.svg` | same mark in the dark-theme accent `#8ab4f8` |
| `icon-mono.svg` | `currentColor` — inherits the container's `color` |
| `icon-on-dark.svg` | accent for dark surfaces when `color` will not be set |
| `favicon.svg` / `favicon.ico` | 32-unit drawing, redrawn for the pixel grid (16/32/48 in the .ico) |
| `wordmark.svg` | lowercase `trivet`, outlines, `currentColor` |
| `lockup.svg` | icon + wordmark, horizontal, `currentColor` |
| `lockup-dark.svg` | lockup in the dark accent |
| `png/` | raster exports: 512, 192, 180, 32, 16, lockup at 1200 |
| `alt-typographic-v1.svg` | rejected direction, kept for the record |

## Construction

The icon is drawn on a 512-unit grid. The surface is a shallow trapezoid — a plane seen
at a slight angle — running 96→416 across the top edge and narrowing to 128→384 at the
bottom. Three supports hang from that bottom edge: the outer two converge inward as they
descend, the centre one barely tapers. Supports are 48 units wide at the top, 44 at the
foot, and stop at y=400 so the mark breathes inside its box.

That trapezoid is the whole idea. A flat bar with legs under it is the Greek letter pi,
and that is what the first pass looked like: the bar overhung the outer legs by 64 units
and the centre leg was perfectly vertical, so at 16px the mark read as π. Narrowing the
plane and slimming the outer legs into a converging stance removes the letterform without
losing the stand.

Clearspace: let **x** be the plane thickness (48 units at 512). Keep **1x** on every side.
Minimum icon size 16px. Below 120px wide, use the icon alone rather than the lockup.

In the horizontal lockup the icon is 104 units high, the gap is 20 units, and the wordmark
cap height is 62 units, optically centred on the icon's vertical centre.

## Wordmark

Lowercase `trivet`, Noto Sans Mono SemiBold, tracking −0.02em, **baked to outlines**. The
first version used `<text>` with a system-font stack; a logo that re-flows in whatever
monospace the viewer happens to have is not a logo, and screenshots would not match
between machines. The outlined file renders identically in every browser, in
`rsvg-convert`, and in print.

## Colour

- Light theme accent: `#1a4fd6`
- Dark theme accent: `#8ab4f8`

`icon.svg` and `favicon.svg` use the light accent. `icon-mono.svg`, `wordmark.svg` and
`lockup.svg` use `currentColor` and inherit their context — set `color` on the container.
No gradients, shadows, filters, or raster content.

## Refused

The magnifying glass. Every search product uses it, which is exactly why it says nothing.
Also refused: a literal three-legged pot stand drawn in perspective — the abstraction is
the mark, the object is not.

`alt-typographic-v1.svg` turned the surface into a `t` crossbar with an ascender above
three feet. It lost because the extra vertical stroke competes with the supports at 16px
and made the stable surface secondary to the initial.
