"""Jeff B4-lite: Jeff vs Jev on the 16 gold queries Jev's full B4 run covered.

  .venv/bin/python scripts/jeff_b4_lite.py --db <fixture copy>   # with the YAAMS_JEV_* Jeff env

A full-corpus Jeff pass is ~81 min/query on an M4 Pro (one decision at a time),
so each query is scored on a sample: Jev's top 200 + the gold + 2,000 random
non-junk candidates (seed 11, same for every query). Jev's scores for the same
pairs come free from Jev's score cache (B4 cached every pair under jev-1.13.0).
Per query: gold rank inside the sample under each model, random items scoring
>= the gold (x ~26.9 extrapolates to the 53.8k corpus), Spearman(Jeff, Jev),
and overlap of the two models' top 10. Writes JEV_DIR/b4_lite.jsonl.
"""
# ponytail: sampled ceiling, not the full-corpus one; run jev_bruteforce under the
# Jeff env for the real thing (~81 min/query) if the sample says it is worth it
import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from autoresearch_retrieval import _load_gold  # noqa: E402

from yaams import jev  # noqa: E402
from yaams.db import open_db  # noqa: E402

JEV_RUN = Path.home() / "brain/feed/eval/jev"
N_RANDOM = 2000


def ranks(scores):
  order = sorted(scores, key=lambda k: -scores[k])
  return {k: i for i, k in enumerate(order, 1)}


def spearman(a, b):
  ks = [k for k in a if k in b]
  ra, rb = ranks({k: a[k] for k in ks}), ranks({k: b[k] for k in ks})
  n = len(ks)
  return 1 - 6 * sum((ra[k] - rb[k]) ** 2 for k in ks) / (n * (n * n - 1)) if n > 2 else float("nan")


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  a = ap.parse_args()
  conn = open_db(a.db, readonly=True)
  gold = {g["query_id"]: g for g in _load_gold(conn)[0]}
  pool = [r[0] for r in conn.execute("SELECT id FROM items WHERE junk_reason IS NULL")]
  pool += [r[0] for r in conn.execute("SELECT id FROM consolidations")]
  rand = random.Random(11).sample(pool, N_RANDOM)
  jev_cache = sqlite3.connect(f"file:{JEV_RUN / 'cache.db'}?mode=ro", uri=True)
  out = jev.JEV_DIR / "b4_lite.jsonl"
  for line in open(JEV_RUN / "b4_full.jsonl"):
    b4 = json.loads(line)
    g = gold[b4["query_id"]]
    state = jev.rel_state(g["text"], (g["ts"] or "")[:10])
    ids = list(dict.fromkeys([b4["gold_id"], *(i for i, _ in b4["top200"]), *rand]))
    texts = jev.rel_texts(conn, ids)
    t0 = time.perf_counter()
    jeff = jev.noul(state, texts, jev.REL_CRITERION, criterion_version="rel-1", tag="jeff_b4_lite")
    wall = time.perf_counter() - t0
    keys = {i: jev.cache_key(state, texts[i][:jev.MAX_CHARS], "rel-1", model="jev-1.13.0") for i in texts}
    jevs = {}
    ks = list(keys.values())
    for s in range(0, len(ks), 500):
      ch = ks[s:s + 500]
      jevs.update(jev_cache.execute(f"SELECT k, v FROM scores WHERE k IN ({','.join('?' * len(ch))})", ch))
    jev_s = {i: jevs[k] for i, k in keys.items() if k in jevs}
    gid = b4["gold_id"]
    row = {"query_id": b4["query_id"], "query": g["text"], "kind": g["kind"], "n": len(texts),
           "jev_covered": len(jev_s), "jev_full_rank": b4["gold_rank"]}
    for name, sc in (("jeff", jeff), ("jev", jev_s)):
      gn = sc.get(gid)
      row[f"{name}_gold_noul"] = gn
      row[f"{name}_rank_in_sample"] = ranks(sc).get(gid)
      row[f"{name}_random_ge_gold"] = None if gn is None else sum(1 for i in rand if sc.get(i, -1) >= gn)
    row["spearman"] = round(spearman(jeff, jev_s), 4)
    top = lambda sc: {k for k, _ in sorted(sc.items(), key=lambda kv: -kv[1])[:10]}  # noqa: E731
    row["top10_overlap"] = len(top(jeff) & top(jev_s))
    row["wall_s"] = round(wall, 1)
    with open(out, "a") as f:
      f.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


if __name__ == "__main__":
  main()
