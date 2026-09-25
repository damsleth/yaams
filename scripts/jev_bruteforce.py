"""Jev B4: brute-force ceiling. Score every non-junk item (and consolidation)
for each gold query with Jev and record where the gold lands. Default criterion
version rel-2: the criterion rides in `state` once, candidates carry a date
prefix; `--criterion-version rel-1` reproduces the pilot format.

  .venv/bin/python scripts/jev_bruteforce.py --db <fixture copy> --queries 5 --max-dollars 1 --tag pilot

Read-only. Consolidations are candidates too: 22 of the 42 golds are `cons:` ids.
nouls come back at 2-decimal granularity, so ranks tie heavily: gold_rank is
optimistic (1 + strictly higher), gold_rank_worst counts the ties against it.
The dollar stop is checked before each chunk against the running estimate and
a stopped query is written with `complete: false`.
Writes ~/brain/feed/eval/jev/b4_<tag>.jsonl. Usage tag jev_b4_<tag>.
"""
import argparse
import heapq
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from autoresearch_retrieval import _load_gold, _split_bucket  # noqa: E402

from yaams import jev  # noqa: E402
from yaams.db import open_db  # noqa: E402

CRITERION = "This item contains evidence that answers the question or directly helps answer it."
OWNER = "Kim (Carl Joakim Damsleth); 'I', 'me', 'my' refer to him"
CHUNK = 4096


def corpus(conn, version="rel-2"):
  """rel-1: `[source | ts | subject]` header. rel-2: date prefix only (plus subject)."""
  docs = {}
  for r in conn.execute("SELECT id, source, timestamp, subject, content FROM items "
                        "WHERE junk_reason IS NULL ORDER BY id"):
    body = (r[4] or "")[:jev.MAX_CHARS]
    if version == "rel-1":
      docs[r[0]] = f"[{r[1]} | {r[2]} | {r[3] or ''}]\n{body}"
    else:
      docs[r[0]] = f"{(r[2] or '')[:10]}{': ' + r[3] if r[3] else ''}\n{body}"
  for r in conn.execute("SELECT id, source, start_timestamp, end_timestamp, summary "
                        "FROM consolidations ORDER BY id"):
    body = r[4][:jev.MAX_CHARS]
    if version == "rel-1":
      docs[r[0]] = f"[{r[1]} | {r[2]} - {r[3]} | consolidation]\n{body}"
    else:
      docs[r[0]] = f"{r[2][:10]}..{r[3][:10]} (thread summary)\n{body}"
  return docs


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  ap.add_argument("--queries", default="all", help="all or first N (by query_id)")
  ap.add_argument("--split", choices=["dev", "test", "all"], default="all")
  ap.add_argument("--max-dollars", type=float, default=7.0)
  ap.add_argument("--tag", default="adhoc")
  ap.add_argument("--workers", type=int, default=8)
  ap.add_argument("--max-questions", type=int, default=jev.MAX_QUESTIONS)
  ap.add_argument("--criterion-version", choices=["rel-1", "rel-2"], default="rel-2",
                  help="rel-1 repeats the criterion per question (pilot); rel-2 keeps it in state only")
  a = ap.parse_args()
  crit = CRITERION if a.criterion_version == "rel-1" else None

  conn = open_db(a.db, readonly=True)
  gold, _, _ = _load_gold(conn)
  gold = sorted((g for g in gold if a.split == "all" or _split_bucket(g["query_id"]) == a.split),
                key=lambda g: g["query_id"])
  if a.queries != "all":
    gold = gold[: int(a.queries)]
  docs = corpus(conn, a.criterion_version)
  ids = list(docs)
  print(f"{len(gold)} queries x {len(docs)} candidates [{a.criterion_version}]", flush=True)

  stats: dict = {}
  spent = 0.0
  est_sent = 0
  out = jev.JEV_DIR / f"b4_{a.tag}.jsonl"
  crit_tok = jev.est_tokens(crit) if crit else 0
  for g in gold:
    state = {"question": g["text"], "asked_on": (g["ts"] or "")[:10], "owner": OWNER,
             "criterion": CRITERION}
    t0 = time.perf_counter()
    tok0 = stats.get("input_tokens", 0)
    scores: dict[str, float] = {}
    complete = True
    for s in range(0, len(ids), CHUNK):
      chunk = {i: docs[i] for i in ids[s:s + CHUNK]}
      est = sum(jev.est_tokens(t) + crit_tok for t in chunk.values())
      # len/3 undercounts this content (pilot: ~1.28x); scale by the ratio observed so far
      ratio = stats["input_tokens"] / est_sent if est_sent and stats.get("input_tokens") else 1.3
      if spent + est * ratio * jev.DOLLARS_PER_TOKEN > a.max_dollars:
        complete = False
        break
      scores.update(jev.noul(state, chunk, crit, criterion_version=a.criterion_version,
                             tag=f"jev_b4_{a.tag}", workers=a.workers,
                             max_questions=a.max_questions, stats=stats))
      est_sent += est
      spent = stats.get("input_tokens", 0) * jev.DOLLARS_PER_TOKEN
    gid = g["result_id"]
    gn = scores.get(gid)
    rank = rank_worst = None
    if gn is not None:
      rank = 1 + sum(1 for v in scores.values() if v > gn)
      rank_worst = sum(1 for v in scores.values() if v >= gn)
    top = heapq.nlargest(200, scores.items(), key=lambda kv: kv[1])
    row = {"query_id": g["query_id"], "query": g["text"], "kind": g["kind"], "gold_id": gid,
           "gold_rank": rank, "gold_rank_worst": rank_worst, "gold_noul": gn,
           "n_scored": len(scores), "n_candidates": len(docs), "complete": complete,
           "input_tokens": stats.get("input_tokens", 0) - tok0,
           "dollars": round((stats.get("input_tokens", 0) - tok0) * jev.DOLLARS_PER_TOKEN, 4),
           "wall_s": round(time.perf_counter() - t0, 1), "top200": [[i, v] for i, v in top]}
    with open(out, "a") as f:
      f.write(json.dumps(row) + "\n")
    print(json.dumps({k: v for k, v in row.items() if k != "top200"}), flush=True)
    if not complete:
      print(f"STOP: --max-dollars {a.max_dollars} reached (spent ${spent:.4f})")
      break
  print(f"total ${spent:.4f}, {stats}")


if __name__ == "__main__":
  main()
