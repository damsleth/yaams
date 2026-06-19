#!/usr/bin/env python3
"""Campaign report (plan step 5): kept wins + net fitness delta from results.tsv.

results.tsv is the append-only experiment log written by the harness. This reads
it and prints the dev-split trajectory: the baseline, each kept win (tag endswith
'-keep'), and the net quality delta. Read-only.

Usage: .venv/bin/python scripts/autoresearch_summary.py [--split dev]
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

TSV = Path(__file__).resolve().parent / "autoresearch_results.tsv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    args = ap.parse_args()
    if not TSV.exists():
        print("no results.tsv yet")
        return 1

    rows = [r for r in csv.DictReader(TSV.open(), delimiter="\t") if r["split"] == args.split]
    if not rows:
        print(f"no {args.split} rows in results.tsv")
        return 1

    def fit(r):
        try:
            return float(r["fitness"])
        except (ValueError, KeyError):
            return 0.0

    baseline = next((r for r in rows if r["tag"].startswith("baseline")), rows[0])
    kept = [r for r in rows if r["tag"].endswith("-keep") and r["status"] == "ok"]
    latest = rows[-1]
    best = max(rows, key=fit)

    print(f"campaign report  (split={args.split}, {len(rows)} runs logged)")
    print(f"  baseline   {baseline['tag']:28} fitness={fit(baseline):.4f}")
    for r in kept:
        print(f"  kept       {r['tag']:28} fitness={fit(r):.4f}  regr={r['regressions']}")
    print(f"  best-seen  {best['tag']:28} fitness={fit(best):.4f}")
    print(f"  latest     {latest['tag']:28} fitness={fit(latest):.4f}")
    print(f"  net delta  baseline -> latest: {fit(latest) - fit(baseline):+.4f}")
    print(f"             baseline -> best:   {fit(best) - fit(baseline):+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
