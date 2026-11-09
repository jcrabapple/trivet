#!/usr/bin/env python3
"""Grid-search ranking weights against the labelled query set.

Candidates are fetched once per query and reused, so the search is over ordering
only, which is the thing the weights control. Reports the best combos plus the
spread, because a 12-query set can rank a bad combo first by luck: a result that
wins nDCG and loses MRR is not a win.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import pathlib

import app as ms
import ranking

HERE = pathlib.Path(__file__).parent
QUERIES = json.loads((HERE / "eval/queries.json").read_text())
K = 10

GRID = {
    "rrf": [0.25, 0.30, 0.40],
    "title": [0.10, 0.16, 0.22],
    "domain": [0.12, 0.16, 0.22],
    "agree": [0.04, 0.08, 0.14],
}


def relevant(url: str, spec: dict) -> int:
    low = url.lower()
    return 1 if any(p.lower() in low for p in spec["expect"]) else 0


def scores_for(gains: list[int]) -> tuple[float, float]:
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains[:K]))
    ideal = sorted(gains, reverse=True)[:K]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    ndcg = dcg / idcg if idcg else 0.0
    mrr = next((1.0 / i for i, g in enumerate(gains, 1) if g), 0.0)
    return ndcg, mrr


async def gather() -> list[tuple[dict, list]]:
    out = []
    for spec in QUERIES:
        data = await ms.search(spec["q"], [s.name for s in ms.ENGINES], 1, K,
                               "moderate", "any", use_cache=True, ranker="rrf")
        out.append((spec, data["merged"]))
    return out


def evaluate(candidates, cfg) -> tuple[float, float]:
    ndcgs, mrrs = [], []
    for spec, merged in candidates:
        ordered = ranking.rank(merged, spec["q"], config=cfg)
        gains = [relevant(r.hit.url, spec) for r in ordered[:K]]
        n, m = scores_for(gains)
        ndcgs.append(n)
        mrrs.append(m)
    return sum(ndcgs) / len(ndcgs), sum(mrrs) / len(mrrs)


async def main() -> None:
    candidates = await gather()
    base = ranking.load_config()
    rows = []
    for rrf, title, domain, agree in itertools.product(
            GRID["rrf"], GRID["title"], GRID["domain"], GRID["agree"]):
        cfg = json.loads(json.dumps(base))
        cfg["weights"].update({"rrf": rrf, "title": title, "domain": domain, "agree": agree})
        ndcg, mrr = evaluate(candidates, cfg)
        rows.append((ndcg, mrr, rrf, title, domain, agree))

    rows.sort(key=lambda r: (-r[0], -r[1]))
    print(f"{'nDCG':>6} {'MRR':>6}  rrf   title domain agree")
    for ndcg, mrr, a, b, c, d in rows[:10]:
        print(f"{ndcg:6.3f} {mrr:6.3f}  {a:<5} {b:<5} {c:<6} {d}")
    print(f"\nspread: nDCG {rows[-1][0]:.3f} .. {rows[0][0]:.3f} over {len(rows)} combos")
    best = rows[0]
    print(f"best: rrf={best[2]} title={best[3]} domain={best[4]} agree={best[5]} "
          f"-> nDCG {best[0]:.3f}, MRR {best[1]:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
