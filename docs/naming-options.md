# Naming options

The name should behave like a bench instrument, not an advertising promise: a result earns its position through independent corroboration, while the scorer remains skeptical of recency, reputation and SEO. I used analogy blending after Gordon and Koestler, importing decision procedures from voting, surveying, RF hardware and assay work rather than decorating search with Greek. The right answer belongs in a terminal and on a personal hostname, not in a pitch deck.

## Deliberation and testimony

| Name | Evocation and fit | Risk | TLD / repo |
|---|---|---|---|
| **Quorate** (KWOR-ate) | A body is quorate when enough independent members are present to decide, making corroboration a precondition for trusting results. | Governance/meeting uses likely; unfamiliar outside them. | `quorate.dev` / `quorate-search` |
| **Assize** (uh-SIZE) | A court sitting to weigh evidence turns each index into testimony rather than an authority whose ranking must be accepted. | Legal-history baggage; sounds like “a size” aloud. | `assize.dev` / `assize-search` |
| **Inquest** | A formal inquiry establishes what happened by comparing accounts, matching the engine’s refusal to accept one index’s account. | InQuest security company; slightly morbid. | `inquest.dev` / `inquest-search` |
| **Handcount** | An independent tally checks the machine’s first answer, a plain-language image for re-ranking rather than relaying API order. | Election-administration associations; little signal-processing depth. | `handcount.dev` / `handcount-search` |

## Rank and sensor fusion

| Name | Evocation and fit | Risk | TLD / repo |
|---|---|---|---|
| **Borda** (BOR-duh) | Borda count aggregates ranked preferences from independent voters, the closest voting-theory analogue to reciprocal rank fusion. | Borda-count libraries; may falsely imply that exact algorithm. | `borda.dev` / `borda-search` |
| **Kemeny** (KEM-uh-nee) | A Kemeny ranking minimizes disagreement with its inputs, fitting a result list built to reconcile competing orders. | Surname/ranking packages; pronunciation needs learning. | `kemeny.dev` / `kemeny-search` |
| **Kalman** (KAL-mun) | A Kalman filter combines noisy independent measurements into one estimate, the cleanest structural analogy for fusing indexes then applying local priors. | Dense filter literature and old packages; bare-name search noise. | `kalman.dev` / `kalman-search` |

## Instruments and signal paths

| Name | Evocation and fit | Risk | TLD / repo |
|---|---|---|---|
| **Vernier** (VUR-nee-er) | A secondary scale refines a coarse reading, just as the local scoring layer refines the fused ranking. | Vernier science-equipment company; pronunciation varies. | `vernier.dev` / `vernier-search` |
| **Backsight** | Surveyors check a position against a known independent point, capturing cross-checking without promising absolute truth. | Surveying software possible; weaker agreement cue. | `backsight.dev` / `backsight-search` |
| **Wheatstone** (WEET-stuhn) | A balanced bridge extracts a measurement from competing electrical paths, an instrument-level metaphor for agreement rather than popularity. | Wheatstone audio consoles; long surname. | `wheatstone.dev` / `wheatstone-search` |
| **Diplexer** (DYE-plex-er) | An RF diplexer puts independent signals onto one path without pretending they originated together. | Clear in search; generic hardware term and implies only two inputs. | `diplexer.dev` / `diplexer-search` |

## Refinement and counterweight

| Name | Evocation and fit | Risk | TLD / repo |
|---|---|---|---|
| **Assay** (uh-SAY) | An assay tests material before assigning value, fitting domain flags, listicle suppression and skepticism toward confident rankings. | Biotech/mining software likely; weak consensus signal. | `assay.dev` / `assay-search` |
| **Winnow** | Separating useful grain from chaff directly evokes removing SEO debris while preserving small-web results. | Several commercial products; common curation metaphor. | `winnow.dev` / `winnow-search` |
| **Trivet** | Independent supports make one stable surface, a compact physical model of results supported by several sources. | Clear in search; indirect metaphor and cookware results. | `trivet.dev` / `trivet-search` |
| **Counterweight** | A counterweight prevents any one mass from dominating, matching the small-web prior and resistance to a single authoritative index. | Broad political/fitness uses; three syllables and less specific. | `counterweight.dev` / `counterweight-search` |

## Top 3

**1. Kalman.** It wins because sensor fusion is the product’s actual shape: independent noisy indexes produce an estimate, then local judgment corrects it. Two syllables, serious and natural beside other engineering tools. It borrows the principle without claiming to implement the filter. The cost is crowded technical literature and probable package collisions; release as `kalman-search`, not bare `kalman`.

**2. Borda.** It is the shortest route to “independent rankings become a collective decision” and survives speech, a hostname and a repository cleanly. Unlike a generic consensus name, it specifically concerns ranked lists. Its cost is precision: readers familiar with voting may expect Borda count rather than reciprocal rank fusion, so the README must name the metaphor explicitly.

**3. Assay.** This best represents the differentiating second pass. Sources supply material; the engine tests it, discounts manipulation and assigns value locally. It feels like a small, uncompromising instrument and avoids agent-infrastructure language. The cost is that corroboration becomes secondary, while biotech and mining uses will compete for attention.

## Rejected lines of thinking

- **Quorum** — The obvious consensus choice; JPMorgan’s Quorum blockchain and extensive governance software make it crowded.
- **Parallax** — Independent viewpoints produce depth, but the name is saturated across frameworks, hardware and design studios.
- **Mux / Crossbar** — Good signal mechanisms, bad collisions: Mux video and Crossbar.io already own adjacent technical territory.
- **Witness / Testify** — in-toto Witness and Go’s Testify are direct OSS collisions; testimony also overpromises truth.
- **Judge / Juror** — Suggests LLM-as-a-judge and agent infrastructure, precisely the wrong reading for a deterministic human-facing tool.

**Biggest naming risk:** choosing a technically apt surname or instrument whose existing literature makes the unqualified project name nearly as unsearchable as “metasearch.”
