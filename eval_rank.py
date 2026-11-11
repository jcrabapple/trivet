#!/usr/bin/env python3
"""Offline ranking evaluation: nDCG@10 and MRR for each ranker over labelled queries.

Ground truth is "the canonical page for this query exists in the candidate set",
matched by URL substring. It is deliberately blunt: it measures whether the right
page is near the top, not whether every position is ideal.

    env -u PYTHONPATH python3 eval_rank.py            # compare rrf vs v1
    env -u PYTHONPATH python3 eval_rank.py --verbose  # per-query positions
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib

from dataclasses import asdict

import app as ms
import merge
import ranking
import reputation

HERE = pathlib.Path(__file__).parent
QUERIES = json.loads((HERE / "eval/queries.json").read_text())
K = 10


def _iter_queries(snapshot: list | None):
    """Yield (spec, frozen_candidates). Frozen candidates make runs comparable.

    Live evaluation refetches from engines whose results drift by the minute, which
    moved nDCG by 0.2 between runs on identical code. A snapshot is the only way an
    A/B of two weight sets means anything.
    """
    if not snapshot:
        for spec in QUERIES:
            yield spec, None
        return
    for row in snapshot:
        hits = [merge.MergedHit(**h) for h in row["merged"]]
        reps = {k: reputation.Reputation(**v) for k, v in (row.get("reps") or {}).items()}
        yield row, (hits, reps)


async def save_snapshot(path: str, engine_set: str = "default") -> None:
    chosen = (list(ms.DEFAULT_ENGINES) if engine_set == "default"
              else [s.name for s in ms.ENGINES])
    rows = []
    for spec in QUERIES:
        data = await ms.search(spec["q"], chosen, 1, K,
                               "moderate", "any", use_cache=True, ranker="rrf")
        rows.append({"q": spec["q"], "expect": spec["expect"],
                     "note": spec.get("note", ""),
                     "merged": [asdict(h) for h in data["merged"]],
                     "reps": {k: asdict(v) for k, v in data["reps"].items()}})
    pathlib.Path(path).write_text(json.dumps(rows, indent=1))
    print(f"snapshot written: {path} ({sum(len(r['merged']) for r in rows)} candidates)")


def is_relevant(url: str, spec: dict) -> int:
    low = url.lower()
    return 1 if any(pat.lower() in low for pat in spec["expect"]) else 0


def ndcg(gains: list[int], k: int = K) -> float:
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))
    ideal_gains = sorted(gains, reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal_gains))
    return dcg / idcg if idcg else 0.0


def mrr(gains: list[int]) -> float:
    for i, g in enumerate(gains, start=1):
        if g:
            return 1.0 / i
    return 0.0


async def evaluate(ranker: str, verbose: bool, snapshot: list | None = None,
                   engine_set: str = "default") -> dict:
    chosen = (list(ms.DEFAULT_ENGINES) if engine_set == "default"
              else [s.name for s in ms.ENGINES])
    ndcgs, mrrs, misses = [], [], []
    for spec, frozen_pair in _iter_queries(snapshot):
        if frozen_pair is not None:
            frozen, reps = frozen_pair
            ordered = (ranking.rank(frozen, spec["q"], reputation=reps)
                       if ranker != "rrf" else sorted(
                           frozen, key=lambda h: (-h.score, h.best_rank)))
            urls = [r.hit.url if hasattr(r, "hit") else r.url for r in ordered][:K]
        else:
            data = await ms.search(spec["q"], chosen, 1, K,
                                   "moderate", "any", use_cache=True, ranker=ranker)
            urls = [h.url for h in data["window"]]
        gains = [is_relevant(u, spec) for u in urls]
        if not any(gains):
            misses.append(spec["q"])
        ndcgs.append(ndcg(gains))
        mrrs.append(mrr(gains))
        if verbose:
            first = next((i for i, g in enumerate(gains, start=1) if g), None)
            print(f"  {spec['q'][:44]:<46} first relevant #{first or '-'}  ndcg={ndcg(gains):.2f}")
    return {"n": len(QUERIES), "ndcg": sum(ndcgs) / len(ndcgs),
            "mrr": sum(mrrs) / len(mrrs), "misses": misses}


async def main() -> None:
    global QUERIES
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--rankers", default="rrf,v1")
    ap.add_argument("--queries", default="eval/queries.json")
    ap.add_argument("--weights", default="", help="JSON file of weight overrides to apply")
    ap.add_argument("--snapshot", default="", help="evaluate against a frozen candidate set")
    ap.add_argument("--save-snapshot", default="", help="write a frozen candidate set and exit")
    ap.add_argument("--engines", default="default", choices=("default", "all"))
    args = ap.parse_args()

    QUERIES = json.loads(pathlib.Path(args.queries).read_text())
    if args.save_snapshot:
        await save_snapshot(args.save_snapshot, args.engines)
        return
    if args.weights:
        override = json.loads(pathlib.Path(args.weights).read_text())
        ranking.DEFAULT_CONFIG["weights"].update(override.get("weights", override))
    snapshot = json.loads(pathlib.Path(args.snapshot).read_text()) if args.snapshot else None
    if snapshot:
        print(f"(frozen candidates: {args.snapshot})")
    results = {}
    for ranker in args.rankers.split(","):
        if args.verbose:
            print(f"\n{ranker}:")
        results[ranker] = await evaluate(ranker, args.verbose, snapshot, args.engines)
    print(f"\n{'ranker':<8} {'nDCG@10':>8} {'MRR':>7}  missed")
    for name, r in results.items():
        print(f"{name:<8} {r['ndcg']:>8.3f} {r['mrr']:>7.3f}  {len(r['misses'])}")
    base = results.get("rrf")
    if base and "v1" in results:
        d = results["v1"]["ndcg"] - base["ndcg"]
        print(f"\nv1 vs rrf: {d:+.3f} nDCG ({'better' if d > 0 else 'worse' if d < 0 else 'equal'})")


if __name__ == "__main__":
    asyncio.run(main())
