"""Jev B4 report: pure-Jev ranking vs the hybrid anchor on the same gold queries.

  .venv/bin/python scripts/jev_b4_report.py --db <fixture copy> --tag full

Replays hybrid per gold through the harness's own `_replay_one` (exclude_junk,
eval depth 50), capturing its final result list to get hybrid's top 50.
Metrics use the harness formulas (hit_rate, mrr, mrr_partial, recall@10,
quality = 0.7*hit_rate + 0.3*mrr_partial). Jev ranks tie at 2-decimal nouls,
so Jev metrics are reported at the optimistic and pessimistic rank.
Writes b4_<tag>_report.json and the owner sheet b4_<tag>_jev_only.tsv
(30 sampled Jev top-10 items that are neither the gold nor in hybrid's top 50).
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).parent))
import autoresearch_retrieval as ar  # noqa: E402

from yaams.cli._shared import _embed_config, _self_identities  # noqa: E402
from yaams.config import load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402
from yaams.enrich import Embedder  # noqa: E402
from yaams.enrich.entities import detect_lang  # noqa: E402
from yaams.jev import JEV_DIR  # noqa: E402
from yaams.retrieve.synonyms import normalize_synonym_groups  # noqa: E402


def metrics(rows, rank_key):
  n = len(rows)
  if not n:
    return {}
  ranks = [r[rank_key] for r in rows]
  corr = [r[rank_key] for r in rows if r["kind"] == "correction"]
  hit = sum(1 for x in ranks if x == 1) / n
  mrr_p = sum(1 / x if x else 0 for x in corr) / len(corr) if corr else 0.0
  return {"hit_rate": round(hit, 4), "mrr": round(sum(1 / x if x else 0 for x in ranks) / n, 4),
          "mrr_partial": round(mrr_p, 4),
          "recall@10": round(sum(1 for x in ranks if x and x <= 10) / n, 4),
          "quality": round(0.7 * hit + 0.3 * mrr_p, 4)}


def hybrid_replay(db, gold_by_q):
  cfg = load_config()
  rc = cfg.get("retrieve")
  syn = normalize_synonym_groups(rc.get("synonyms") if isinstance(rc, dict) else None)
  holder = {}

  def capture(fn):
    def w(*a, **k):
      holder["res"] = fn(*a, **k)
      return holder["res"]
    return w

  ar.run_query = capture(ar.run_query)
  ar.filter_results_by_entities = capture(ar.filter_results_by_entities)
  conn = open_db(db, readonly=True)
  emb = Embedder(**_embed_config(cfg), quiet=True)
  self_ids = _self_identities(cfg)
  out = {}
  for qid, g in gold_by_q.items():
    rank, _ = ar._replay_one(conn, emb, self_ids, g, syn, exclude_junk=True)
    out[qid] = (rank, [r.id for r in holder["res"]][:50])
  return conn, out


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  ap.add_argument("--tag", default="full")
  a = ap.parse_args()
  b4 = {}
  for line in open(JEV_DIR / f"b4_{a.tag}.jsonl"):
    r = json.loads(line)
    b4[r["query_id"]] = r  # last row per query wins
  conn0 = open_db(a.db, readonly=True)
  gold, _, _ = ar._load_gold(conn0)
  gold_by_q = {g["query_id"]: g for g in gold if g["query_id"] in b4}
  conn, hyb = hybrid_replay(a.db, gold_by_q)

  rows = []
  for qid, r in b4.items():
    h_rank, h_top = hyb[qid]
    rows.append({**{k: v for k, v in r.items() if k != "top200"},
                 "hybrid_rank": h_rank, "hybrid_top50": h_top, "top200": r["top200"],
                 "lang": "nb" if detect_lang(r["query"]) == "no" else "en",
                 "split": ar._split_bucket(qid)})
  rep = {"n": len(rows), "incomplete": [r["query_id"] for r in rows if not r["complete"]],
         "hybrid": metrics(rows, "hybrid_rank"), "jev_best": metrics(rows, "gold_rank"),
         "jev_worst": metrics(rows, "gold_rank_worst")}
  for sp in ("dev", "test"):
    s = [r for r in rows if r["split"] == sp]
    rep[f"{sp}_n"] = len(s)
    rep[f"{sp}_hybrid"], rep[f"{sp}_jev_best"] = metrics(s, "hybrid_rank"), metrics(s, "gold_rank")
  nouls = sorted(r["gold_noul"] for r in rows if r["gold_noul"] is not None)
  rep["gold_noul"] = {"median": median(nouls), "below_0.5": sum(x < 0.5 for x in nouls), "all": nouls}
  rep["jev_blind"] = [(r["query"], r["kind"], r["gold_id"][:12], r["gold_noul"], r["gold_rank"], r["hybrid_rank"])
                      for r in rows if (r["gold_noul"] or 0) < 0.5]
  rep["hybrid_blind"] = [(r["query"], r["hybrid_rank"], r["gold_rank"], r["gold_noul"])
                         for r in rows if not r["hybrid_rank"] or r["hybrid_rank"] > 50]
  rep["hybrid_blind_jev_top10"] = sum(1 for _, _, jr, _ in rep["hybrid_blind"] if jr and jr <= 10)
  rep["per_query"] = [(r["query"], r["kind"], r["lang"], r["gold_rank"], r["gold_rank_worst"],
                       r["hybrid_rank"], r["gold_noul"], r["dollars"], r["wall_s"]) for r in rows]
  for lg in ("en", "nb"):
    s = [r for r in rows if r["lang"] == lg]
    if s:
      rep[f"cost_{lg}"] = {"n": len(s), "dollars_mean": round(sum(r["dollars"] for r in s) / len(s), 4),
                           "wall_s_mean": round(sum(r["wall_s"] for r in s) / len(s), 1)}

  finds = []
  for r in rows:
    hs = set(r["hybrid_top50"])
    for i, v in r["top200"][:10]:
      if i != r["gold_id"] and i not in hs:
        finds.append((r, i, v))
  rep["jev_only_finds"] = len(finds)
  random.seed(7)
  sample = random.sample(finds, min(30, len(finds)))
  with open(JEV_DIR / f"b4_{a.tag}_jev_only.tsv", "w", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["n", "query", "asked_on", "jev_noul", "item_text", "answers? (Y/N)", "query_id", "item_id"])
    for n, (r, i, v) in enumerate(sample, 1):
      if i.startswith("cons:"):
        t = conn.execute("SELECT start_timestamp, summary FROM consolidations WHERE id=?", (i,)).fetchone()
      else:
        t = conn.execute("SELECT timestamp, content FROM items WHERE id=?", (i,)).fetchone()
      text = " / ".join(ln.strip() for ln in (t[1] or "")[:400].splitlines() if ln.strip())
      w.writerow([n, r["query"], gold_by_q[r["query_id"]]["ts"][:10], v, f"{t[0][:10]}: {text}", "",
                  r["query_id"], i])
  (JEV_DIR / f"b4_{a.tag}_report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
  print(json.dumps({k: v for k, v in rep.items() if k not in ("per_query", "gold_noul")}, indent=1,
                   ensure_ascii=False))
  print("per query: query | kind | lang | jev rank (best-worst) | hybrid | noul | $ | s")
  for q in rep["per_query"]:
    print("  " + " | ".join(str(x) for x in q))


if __name__ == "__main__":
  main()
