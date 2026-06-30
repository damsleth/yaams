#!/usr/bin/env python3
"""Item 06: rerank candidate-pool sweep (.plans/06-rerank-pool-sweep.md).

Drives the autoresearch harness at a ladder of --rerank-k values (plus a
rerank-off baseline) over the dev gold set, tabulates mrr / hit_rate /
recall@10 / p95, and prints the knee + a ship/kill verdict. The harness
(autoresearch_retrieval.py) owns the gold set and the MRR scoring; this only
sweeps the pool size and compares.

The rerank rows need the reranker model (config retrieve.rerank.model) to be
available; the baseline row needs no model. If the rerank rows can't run, the
table shows them as "did not run" and the verdict says so — no fake numbers.

Run:         python scripts/rerank_sweep.py
Self-check:  python scripts/rerank_sweep.py --self-check   (no DB/model needed)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

LADDER = [24, 36, 50, 60, 100]
_HARNESS = Path(__file__).with_name("autoresearch_retrieval.py")
_METRICS = ("mrr", "hit_rate", "recall@10", "retrieval_p95_ms")


def _run_harness(rerank_k: int | None, split: str) -> dict:
  cmd = [sys.executable, str(_HARNESS), "--no-write", "--json", "--split", split]
  if rerank_k:
    cmd += ["--rerank-k", str(rerank_k)]
  out = subprocess.run(cmd, capture_output=True, text=True)
  try:
    return json.loads(out.stdout)
  except Exception:
    return {"status": "crash", "error": (out.stderr or out.stdout or "no output")[-300:]}


def _knee(rows: list[dict], tol: float = 0.01) -> int | None:
  """Smallest rerank_k whose mrr is within `tol` (relative) of the best mrr."""
  scored = [(r["rerank_k"], r["mrr"]) for r in rows if r.get("mrr") is not None]
  if not scored:
    return None
  best = max(m for _, m in scored)
  for k, m in sorted(scored):
    if m >= best * (1.0 - tol):
      return k
  return None


def _verdict(baseline_mrr: float, rows: list[dict]) -> str:
  rk = [r["mrr"] for r in rows if r.get("mrr") is not None]
  if not rk:
    return "NO DATA — rerank rows did not run (reranker model unavailable?). Keep enabled:false."
  best, lo = max(rk), min(rk)
  if best <= baseline_mrr + 1e-9:
    return (f"KILL — best rerank mrr {best:.4f} <= baseline {baseline_mrr:.4f}. "
            "Rerank does not help on this corpus; keep retrieve.rerank.enabled:false.")
  if best - lo < 0.01:
    return (f"FLAT — mrr barely moves across the pool ({lo:.4f}..{best:.4f}); corpus likely "
            "too small for two-stage retrieval (report kill-criterion). Keep rerank opt-in.")
  return (f"SHIP — rerank lifts mrr {baseline_mrr:.4f} -> {best:.4f} (knee at k={_knee(rows)}); "
          "set retrieve.rerank.k to the knee and consider enabled:true.")


def _print_table(baseline: dict, rows: list[dict]) -> None:
  print(f"\n{'pool_k':>8}  {'mrr':>7}  {'hit_rate':>8}  {'recall@10':>9}  {'p95_ms':>8}")
  print("-" * 50)
  print(f"{'off':>8}  {baseline.get('mrr', 0):>7.4f}  {baseline.get('hit_rate', 0):>8.4f}  "
        f"{baseline.get('recall@10', 0):>9.4f}  {baseline.get('retrieval_p95_ms', 0):>8.1f}")
  for r in rows:
    if r.get("mrr") is None:
      print(f"{r['rerank_k']:>8}  did not run: {str(r.get('error'))[:48]}")
      continue
    print(f"{r['rerank_k']:>8}  {r['mrr']:>7.4f}  {r['hit_rate']:>8.4f}  "
          f"{r.get('recall@10', 0):>9.4f}  {r['retrieval_p95_ms']:>8.1f}")


def _self_check() -> int:
  rows = [
    {"rerank_k": 24, "mrr": 0.40, "hit_rate": 0.30, "retrieval_p95_ms": 60},
    {"rerank_k": 36, "mrr": 0.50, "hit_rate": 0.40, "retrieval_p95_ms": 80},
    {"rerank_k": 50, "mrr": 0.505, "hit_rate": 0.40, "retrieval_p95_ms": 100},
    {"rerank_k": 100, "mrr": 0.506, "hit_rate": 0.40, "retrieval_p95_ms": 200},
  ]
  assert _knee(rows) == 50, _knee(rows)          # 0.505 within 1% of 0.506; 36 (0.50) is not
  assert _verdict(0.30, rows).startswith("SHIP")
  assert _verdict(0.60, rows).startswith("KILL")
  flat = [{"rerank_k": k, "mrr": 0.42, "retrieval_p95_ms": 50} for k in LADDER]
  assert _verdict(0.40, flat).startswith("FLAT")
  assert _verdict(0.40, [{"rerank_k": 24, "mrr": None}]).startswith("NO DATA")
  assert _knee([]) is None
  print("rerank_sweep self-check: OK")
  return 0


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--self-check", action="store_true",
                  help="Validate tabulation/verdict logic; no DB or model needed.")
  ap.add_argument("--split", default="dev")
  ap.add_argument("--ladder", default=",".join(str(k) for k in LADDER),
                  help="Comma-separated rerank_k values to sweep.")
  args = ap.parse_args()
  if args.self_check:
    return _self_check()

  ladder = [int(x) for x in args.ladder.split(",") if x.strip()]
  print("baseline (rerank off) ...", flush=True)
  baseline = _run_harness(None, args.split)
  if baseline.get("status") != "ok":
    print(f"baseline failed: {baseline.get('error')}", file=sys.stderr)
    return 1
  rows: list[dict] = []
  for k in ladder:
    print(f"rerank-k {k} ...", flush=True)
    r = _run_harness(k, args.split)
    rows.append({"rerank_k": k, "error": r.get("error"),
                 **{m: r.get(m) for m in _METRICS}})
  _print_table(baseline, rows)
  print("\n" + _verdict(baseline.get("mrr", 0.0), rows))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
