"""Which engine set is actually worth its latency?

Fetches every labelled query against every engine **once**, freezes that to a
snapshot, then scores all 2^n - 1 engine subsets offline. Refetching per subset
would make the comparison meaningless (engine results drift between runs).

    env -u PYTHONPATH /usr/bin/python3 eval/combo_bench.py --fetch   # slow, spends money
    env -u PYTHONPATH /usr/bin/python3 eval/combo_bench.py           # offline, instant
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import merge as merge_mod  # noqa: E402
import providers  # noqa: E402
import ranking  # noqa: E402

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "combo-snapshot.json"
ENGINES = ("keenable", "exa", "brave", "tavily", "serper", "linkup", "firecrawl")
K = 10
FETCH_LIMIT = 20


def load_queries() -> list[dict]:
    out = []
    for name in ("queries.json", "holdout.json"):
        for row in json.loads((ROOT / name).read_text()):
            out.append({**row, "set": "tuning" if name == "queries.json" else "holdout"})
    return out


async def fetch_all() -> None:
    env = providers.load_env()
    queries = load_queries()
    specs = {s.name: s for s in providers.SPECS}
    async with httpx.AsyncClient(timeout=25.0, headers={"User-Agent": providers.UA}) as client:

        async def one(spec_name: str, q: str) -> tuple[str, str, dict]:
            spec = specs[spec_name]
            # run_engine is the real path: it applies each engine's own rate limit.
            # Calling spec.fn directly made Brave fail 35% of the time against its
            # 1 rps plan and Firecrawl fail 60%, which reads as "bad engine" noise.
            res = await providers.run_engine(
                client, spec, q, FETCH_LIMIT, env, 25.0,
                providers.QueryOpts(time_range="any", safesearch="moderate"))
            return q, spec_name, {
                "ok": res.ok, "ms": res.ms, "error": res.error,
                "hits": [{"title": h.title, "url": h.url, "snippet": h.snippet,
                          "rank": h.rank, "published": h.published} for h in res.hits],
            }

        # One query at a time, fanning out to every engine: exactly what a real
        # page load does. Firing all queries at once tripped Brave's and Firecrawl's
        # rate limits (429 on 16/26 Firecrawl calls) and measured our own burst.
        by_query: dict[str, dict] = {}
        for i, row in enumerate(queries, 1):
            entry = {"q": row["q"], "set": row["set"], "expect": row["expect"], "engines": {}}
            for q, name, payload in await asyncio.gather(
                    *[one(name, row["q"]) for name in ENGINES]):
                entry["engines"][name] = payload
            by_query[row["q"]] = entry
            if i % 5 == 0:
                print(f"  {i}/{len(queries)} queries", file=sys.stderr)
    # write via a temp file: a partially written snapshot that another process then
    # reads is worse than no snapshot, and two concurrent fetches used to clobber
    # each other mid-run
    tmp = SNAPSHOT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(list(by_query.values()), indent=1))
    os.replace(tmp, SNAPSHOT)
    print(f"wrote {SNAPSHOT} ({len(by_query)} queries x {len(ENGINES)} engines)")


def _results(row: dict, names: tuple[str, ...]) -> list:
    out = []
    for name in names:
        payload = row["engines"].get(name) or {}
        hits = [providers.Hit(title=h["title"], url=h["url"], snippet=h["snippet"],
                              engine=name, rank=h["rank"], published=h.get("published"))
                for h in payload.get("hits", [])]
        out.append(providers.EngineResult(name=name, ok=payload.get("ok", False),
                                          hits=hits, ms=payload.get("ms", 0),
                                          error=payload.get("error")))
    return out


def _ndcg(urls: list[str], expect: list[str], k: int = K) -> float:
    gains = [1 if any(e in u for e in expect) else 0 for u in urls[:k]]
    if not gains or not any(gains):
        return 0.0
    dcg = sum(g / __import__("math").log2(i + 2) for i, g in enumerate(gains))
    idcg = sum(1 / __import__("math").log2(i + 2) for i in range(min(len(gains), sum(gains))))
    return dcg / idcg


def evaluate(rows: list[dict], names: tuple[str, ...]) -> dict:
    scores, lat, ok_ratio = [], [], []
    for row in rows:
        results = _results(row, names)
        merged = merge_mod.merge(results, collapse_syndicated=False)
        ranked = ranking.rank(merged, row["q"])
        scores.append(_ndcg([r.hit.url for r in ranked], row["expect"]))
        lat.append(max(r.ms for r in results) if results else 0)
        ok_ratio.append(sum(1 for r in results if r.ok) / len(results))
    return {"ndcg": statistics.mean(scores), "p_max_ms": int(statistics.mean(lat)),
            "ok": round(100 * statistics.mean(ok_ratio), 1), "n": len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="refetch the frozen snapshot")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    lock = SNAPSHOT.with_suffix(".lock")
    if args.fetch:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            sys.exit(f"{lock} exists: another fetch is running (delete it if that is stale)")
        try:
            os.write(fd, str(os.getpid()).encode())
            asyncio.run(fetch_all())
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)
    elif not SNAPSHOT.exists():
        asyncio.run(fetch_all())
    rows = json.loads(SNAPSHOT.read_text())
    tuning = [r for r in rows if r["set"] == "tuning"]
    holdout = [r for r in rows if r["set"] == "holdout"]

    print(f"snapshot: {len(tuning)} tuning + {len(holdout)} holdout queries, "
          f"{len(ENGINES)} engines\n")

    singles = {}
    for name in ENGINES:
        singles[name] = evaluate(rows, (name,))
    print("single engine")
    for name, s in sorted(singles.items(), key=lambda kv: -kv[1]["ndcg"]):
        print(f"  {name:10} nDCG {s['ndcg']:.3f}  worst-call {s['p_max_ms']:>5}ms  ok {s['ok']:>5}%")

    combos = []
    for size in range(2, len(ENGINES) + 1):
        for names in itertools.combinations(ENGINES, size):
            s = evaluate(tuning, names)
            h = evaluate(holdout, names)
            combos.append({"names": names, **s, "holdout": h["ndcg"]})
    combos.sort(key=lambda c: (-c["ndcg"], c["p_max_ms"]))
    print(f"\ntop {args.top} sets by tuning nDCG (holdout in brackets, mean worst-engine latency)")
    for c in combos[:args.top]:
        print(f"  {c['ndcg']:.3f} [{c['holdout']:.3f}] {c['p_max_ms']:>5}ms  {'+'.join(c['names'])}")

    best = combos[0]
    print(f"\nmarginal value: best set = {'+'.join(best['names'])}")
    for name in ENGINES:
        if name in best["names"]:
            continue
        trial = evaluate(tuning, best["names"] + (name,))
        print(f"  + {name:10} {trial['ndcg'] - best['ndcg']:+.4f} nDCG, "
              f"{trial['p_max_ms'] - best['p_max_ms']:+d}ms")
    for name in best["names"]:
        trial = evaluate(tuning, tuple(n for n in best["names"] if n != name))
        print(f"  - {name:10} {trial['ndcg'] - best['ndcg']:+.4f} nDCG, "
              f"{trial['p_max_ms'] - best['p_max_ms']:+d}ms")


if __name__ == "__main__":
    main()
