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
CAMPAIGN = Path(__file__).resolve().parent / "autoresearch_campaign.tsv"


def _campaign_report() -> None:
    """Per-experiment improve/regress table from the loop's stats file."""
    if not CAMPAIGN.exists():
        return
    rows = list(csv.DictReader(CAMPAIGN.open(), delimiter="\t"))
    if not rows:
        return

    def fl(r, k):
        try:
            return float(r[k])
        except (ValueError, KeyError, TypeError):
            return 0.0

    wins = [r for r in rows if r.get("verdict") == "WIN"]
    improved = [r for r in rows if fl(r, "delta") > 0 and r.get("verdict") != "WIN"]
    regressed = [r for r in rows if int(r.get("regressions") or 0) > 0 or fl(r, "delta") < 0]
    print(f"\nexperiment log  ({len(rows)} experiments across {len({r['round'] for r in rows})} rounds)")
    print(f"  WIN (kept):        {len(wins)}")
    print(f"  improved (not kept): {len(improved)}")
    print(f"  regressed/worse:   {len(regressed)}")
    print(f"  {'round':>5} {'delta':>8} {'regr':>4} {'verdict':>8}  key")
    for r in sorted(rows, key=lambda r: fl(r, "delta"), reverse=True):
        print(f"  {r.get('round',''):>5} {fl(r,'delta'):>+8.4f} {r.get('regressions',''):>4} "
              f"{r.get('verdict',''):>8}  {r.get('key','')}")


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
    _campaign_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
