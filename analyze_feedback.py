#!/usr/bin/env python3
"""Read the feedback log: position bias, per-domain CTR, and offline replay.

    env -u PYTHONPATH python3 analyze_feedback.py                 # report
    env -u PYTHONPATH python3 analyze_feedback.py --replay eval/tuned-v1b.json
    env -u PYTHONPATH python3 analyze_feedback.py --include-test

Replay re-scores the *stored feature vectors* of pages that already happened, so a
weight change is evaluated on real clicks without refetching a single result.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

import feedback
import ranking

MIN_CLICKS_FOR_REPLAY = 10


def score_from_features(features: dict[str, Any], cfg: dict[str, Any], intent: str) -> float:
    w = cfg["weights"]
    fresh_w = w["fresh"] if intent == "recency" else w["fresh"] * 0.2
    return (w["rrf"] * features["rrf"]
            + w["rank"] * features["rank"]
            + w["agree"] * features["agree"]
            + w["title"] * features["title"]
            + w["snippet"] * features["snippet"]
            + w["domain"] * features["domain"]
            + fresh_w * features["fresh"]
            + w["quality"] * features["quality"]
            - features["penalties"])


def report(include_test: bool) -> None:
    t = feedback.totals(include_test)
    print(f"impressions {t['impressions']}  clicks {t['clicks']}  pages {t['pages']}")
    if not t["impressions"]:
        print("\nNo feedback yet. Browse the engine, click a few results, come back.")
        return

    print("\nposition   shown  clicks   ctr   examination")
    for row in feedback.position_report(include_test):
        print(f"{row['position']:>7} {row['impressions']:>7} {row['clicks']:>7} "
              f"{row['ctr']:>6.2f} {row['exam']:>10}")

    rows = feedback.domain_report(include_test)
    seen = [r for r in rows if r["impressions"] >= 3]
    if seen:
        print("\ndomain                     shown clicks   ctr  smoothed")
        for r in sorted(seen, key=lambda r: -r["smoothed_ctr"])[:12]:
            print(f"{r['domain'][:24]:<24} {r['impressions']:>6} {r['clicks']:>6} "
                  f"{r['ctr']:>6.2f} {r['smoothed_ctr']:>9.2f}")

    # Inverse-propensity estimate: weight each click by 1/P(examined at that position).
    # Unbiased in theory, wildly high-variance in practice, so it is withheld until
    # there are enough clicks to mean anything.
    rows = feedback.position_report(include_test)
    impr = sum(r["impressions"] for r in rows)
    if impr:
        raw = t["clicks"] / impr
        if t["clicks"] < 30:
            print(f"\nCTR raw {raw:.3f} | debiased estimate withheld "
                  f"({t['clicks']} clicks; IPS needs a few hundred to be stable)")
        else:
            debiased = sum(r["clicks"] / max(0.05, r["exam"]) for r in rows) / impr
            print(f"\nCTR raw {raw:.3f} | examination-weighted {debiased:.3f} "
                  f"(raw overstates top results; the weighted figure is the honest one)")


def replay(weights_path: str, include_test: bool) -> None:
    pages = feedback.load_replay_set(include_test)
    if not pages:
        print("No clicked pages logged yet; replay needs click data.")
        return
    if len(pages) < MIN_CLICKS_FOR_REPLAY:
        print(f"Only {len(pages)} clicked pages. Below {MIN_CLICKS_FOR_REPLAY} this is "
              f"anecdote, not evaluation. Printing anyway, treat with suspicion.\n")

    configs = {"served": None}
    if weights_path:
        override = json.loads(pathlib.Path(weights_path).read_text())
        cfg = ranking.load_config()
        cfg["weights"].update(override.get("weights", override))
        configs[pathlib.Path(weights_path).stem] = cfg
    configs["default-v1"] = ranking.load_config()

    print(f"{'config':<16} {'MRR(clicked)':>13} {'top1 click':>11} {'top3 click':>11}")
    for name, cfg in configs.items():
        rr_sum, top1, top3 = 0.0, 0, 0
        for page in pages:
            items = page["items"]
            if cfg is None:
                # served order is the stored position order; re-scoring it would
                # measure the current weights, not what the user actually saw
                order = sorted(items, key=lambda i: i["position"])
            else:
                order = sorted(items, key=lambda i: -score_from_features(
                    i["features"], cfg, i["features"].get("intent", "general")))
            rank_of_click = next((n for n, it in enumerate(order, 1)
                                  if it["position"] in page["clicked"]), None)
            if rank_of_click:
                rr_sum += 1.0 / rank_of_click
                top1 += 1 if rank_of_click == 1 else 0
                top3 += 1 if rank_of_click <= 3 else 0
        n = len(pages)
        print(f"{name:<16} {rr_sum / n:>13.3f} {top1 / n:>11.3f} {top3 / n:>11.3f}")
    print(f"\n{n} clicked pages. MRR(clicked) = where the thing you clicked would land "
          f"under each config.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default="", help="weights JSON to compare against served order")
    ap.add_argument("--include-test", action="store_true")
    args = ap.parse_args()
    report(args.include_test)
    if args.replay is not None:
        print()
        replay(args.replay, args.include_test)


if __name__ == "__main__":
    main()
