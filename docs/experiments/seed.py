#!/usr/bin/env python3
"""One-shot reconstruction of the autoresearch experiment history.

Reads the two committed ledgers (scripts/autoresearch_campaign.tsv and
scripts/autoresearch_results.tsv) plus a few facts that only live in git
history, and writes docs/experiments/experiments.jsonl — the canonical chart
dataset. After this initial seed, experiments.jsonl is the source of truth:
new experiments append a line there, not here. This script stays in the repo
for provenance (how the pre-2026-06-30 history was reconstructed).

Run:  python docs/experiments/seed.py     (rewrites experiments.jsonl)

Honesty notes baked in:
- The campaign 'mrr' is partial-credit MRR; the rerank sweep 'mrr' is full
  rank-1 MRR on a different gold set. They are NOT comparable — each row
  carries its `era` (gold-set version) so the viewer can band them.
- tier2_boost_fused_order_cap shows a WIN in the campaign TSV but was reverted
  as a held-out overfit (git 9dec8bf, e65cdfd). Final disposition: kill.
- The autoresearch loop writes the TSVs; this maps them to the chart schema.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = ROOT / "scripts" / "autoresearch_campaign.tsv"
RESULTS = ROOT / "scripts" / "autoresearch_results.tsv"
OUT = Path(__file__).with_name("experiments.jsonl")

# Gold-set eras. Each experiment is tagged with the era it was measured in so
# the viewer can draw boundaries and never connect a line across a regime shift.
ERA_SPARSE = "≈46 gold (phase 2)"
ERA_58 = "58 gold (jun19)"
ERA_78A = "78 gold (jun20)"
ERA_78B = "78 gold (jun21)"
ERA_RERANK = "rerank sweep (jun30, full-MRR)"

# Reverted-as-overfit experiments: campaign says WIN, final truth is kill.
OVERFIT_REVERTED = {"tier2_boost_fused_order_cap"}

# status/verdict tokens that mean "kept as the new accepted baseline".
KEEP_TOKENS = {"keep", "win", "WIN"}


def _f(v: str) -> float | None:
  v = (v or "").strip()
  if v in ("", "-", "nan"):
    return None
  try:
    return float(v)
  except ValueError:
    return None


def _disposition(status: str, verdict: str, key: str) -> str:
  if key in OVERFIT_REVERTED:
    return "kill"
  if status in KEEP_TOKENS or verdict in KEEP_TOKENS:
    return "keep"
  return "kill"


rows: list[dict] = []
seq = 0


def add(**row):
  global seq
  seq += 1
  row["seq"] = seq
  rows.append(row)


# --- Phase 2 parameter sweep (sparse gold set), from results.tsv ------------
# Only the early experiment rows; the jun20-21 experiments are taken from the
# richer campaign.tsv below, and the repeated baseline/anchor churn rows are
# collapsed to one era anchor each.
PHASE2_KEYS = {
  "baseline-fts", "baseline-hybrid", "baseline-hybrid-dev", "baseline-fts-dev",
  "rrf30-keep", "cb1.0", "cb1.3", "cb2.0", "pk100", "pk120", "pk80-keep",
  "fts-prefix", "prefix5-keep", "bm25w-s2-keep", "bm25w-s3", "bm25w-s2.5",
  "bm25w-p2", "subjtok-1.1", "subjtok-1.05", "subjtok-1.08", "subjtok-all-1.1",
  "subjtok-all-1.07", "subjtok-cons-1.1", "recency-f0.9", "veck-1.5",
  "veck-1.25", "veck-0.75",
}

with RESULTS.open() as f:
  for r in csv.DictReader(f, delimiter="\t"):
    tag = r["tag"]
    if tag not in PHASE2_KEYS:
      continue
    status = r["status"]
    is_baseline = tag.startswith("baseline")
    if is_baseline:
      disp = "baseline"
    elif tag.endswith("-keep") and status == "ok":
      disp = "keep"
    else:
      disp = "kill"
    add(
      key=tag.replace("-keep", ""),
      date="2026-06-18",
      era=ERA_SPARSE,
      disposition=disp,
      status=status,
      delta=None,
      note=f"mode={r['mode']} split={r['split']} regressions={r['regressions']}",
      commit="",
      metrics={
        "quality": _f(r["fitness"]),
        "hit_rate": _f(r["hit_rate"]),
        "mrr": _f(r["mrr_partial"]),
        "recall@10": None,
        "latency_p95_ms": _f(r["p95_ms"]),
      },
    )

# --- jun19 wins not in the campaign TSV (boost3.0, temporal de-boost) --------
# These were measured on the 58-gold set before the medium campaign began.
JUN19 = [
  ("boost3.0", "keep", 0.5109, 0.587, 0.3531, 168.5,
   "entity boost 1.5->3.0; q held, mrr up"),
  ("temporal_range_deboost", "keep", 0.5243, 0.6034, 0.3629, 153.6,
   "narrow-window consolidation de-boost; q 0.5121->0.5243"),
]
for key, disp, q, hr, mrr, p95, note in JUN19:
  add(key=key, date="2026-06-19", era=ERA_58, disposition=disp, status="keep",
      delta=None, note=note, commit="",
      metrics={"quality": q, "hit_rate": hr, "mrr": mrr,
               "recall@10": None, "latency_p95_ms": p95})

# --- The medium campaign (jun20-21), from campaign.tsv ----------------------
# Era split: rounds before the jun21 re-freeze (gold 68->78) are jun20; the
# tier2_factual_coverage_recovery round onward is jun21. We infer the split
# from the hit_rate plateau (0.6034 -> 0.5909/0.6364 after the re-freeze).
seen_campaign_keys: set[str] = set()
JUN21_ONSET = {"graded_rank_agreement", "tier2_factual_coverage_recovery",
               "dual_coverage_disagreement_demote", "consolidation_boost_coverage_gated",
               "thread_coherence_credit", "tier2_boost_fused_order_cap",
               "sole_fts_exact_over_vec_only_tiebreak", "cons_breadth_mass_normalize",
               "vector_distance_margin_tiebreak"}

with CAMPAIGN.open() as f:
  for r in csv.DictReader(f, delimiter="\t"):
    key = r["key"]
    # The loop re-tries some ideas across rounds; keep the row that reached a
    # verdict (win/keep), else the last seen attempt.
    disp = _disposition(r["status"], r["verdict"], key)
    era = ERA_78B if key in JUN21_ONSET else ERA_78A
    date = "2026-06-21" if era == ERA_78B else "2026-06-20"
    note = r["note"]
    if key in OVERFIT_REVERTED:
      note = "REVERTED as held-out overfit (git 9dec8bf). " + note
    rec = dict(
      key=key, date=date, era=era, disposition=disp, status=r["status"],
      delta=_f(r["delta"]), note=note, commit="",
      metrics={
        "quality": _f(r["quality"]),
        "hit_rate": _f(r["hit_rate"]),
        "mrr": _f(r["mrr"]),
        "recall@10": None,
        "latency_p95_ms": _f(r["p95"]),
      },
    )
    # Prefer a win/keep row over an earlier discarded attempt of the same key.
    prior = next((x for x in rows if x["key"] == key and x["era"].startswith("78")), None)
    if prior is not None:
      if disp == "keep" and prior["disposition"] != "keep":
        rows[rows.index(prior)] = {**rec, "seq": prior["seq"]}
      continue
    add(**rec)

# recall@10 was logged for the two jun21 wins.
for x in rows:
  if x["key"] in ("thread_coherence_credit", "tier2_boost_fused_order_cap"):
    x["metrics"]["recall@10"] = 0.9242

# --- Item 06 rerank sweep (jun30) -------------------------------------------
# Different gold set + full-MRR metric => its own era. All kill (rerank
# regresses every rung). The 'off' row is the hybrid baseline it was judged
# against.
RERANK = [
  ("rerank-off (hybrid baseline)", "baseline", 0.7454, 0.6667, 0.9242, 158.0),
  ("rerank-k24", "kill", 0.5367, 0.4091, 0.8182, 13086.4),
  ("rerank-k36", "kill", 0.5327, 0.4091, 0.8030, 4861.2),
  ("rerank-k50", "kill", 0.5078, 0.3636, 0.8030, 5422.9),
  ("rerank-k60", "kill", 0.5053, 0.3636, 0.8030, 6378.5),
  ("rerank-k100", "kill", 0.4991, 0.3636, 0.7727, 10364.2),
]
for key, disp, mrr, hr, rec10, p95 in RERANK:
  add(key=key, date="2026-06-30", era=ERA_RERANK, disposition=disp,
      status="kill" if disp == "kill" else "ok", delta=None,
      note="cross-encoder rerank regresses MRR + adds 5-13s latency; opt-in stays off",
      commit="2b0cf17",
      metrics={"quality": None, "hit_rate": hr, "mrr": mrr,
               "recall@10": rec10, "latency_p95_ms": p95})

with OUT.open("w") as f:
  for r in rows:
    f.write(json.dumps(r, ensure_ascii=False) + "\n")

keeps = sum(1 for r in rows if r["disposition"] == "keep")
kills = sum(1 for r in rows if r["disposition"] == "kill")
base = sum(1 for r in rows if r["disposition"] == "baseline")
print(f"wrote {len(rows)} experiments -> {OUT}")
print(f"  keep={keeps}  kill={kills}  baseline={base}")
