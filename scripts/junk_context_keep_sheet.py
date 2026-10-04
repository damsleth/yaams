"""Owner sheet of context-keep candidates for training (v3.1 Phase 0.1b), mined without model scores.

  .venv/bin/python scripts/junk_context_keep_sheet.py --db <snapshot> [--n 50]

Candidates: training-pool rows (A1 scope) that the owner-bar Copilot relabel calls
KEEP and that reply to a question in the previous message (prev contains '?'),
i.e. replies whose value may live in the exchange. Excluded: every owner-labelled
row and its prev/next neighbours, and every test-2 row. Training only: never scored.
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from junk_sonnet_pass import load_ckpt  # noqa: E402

from yaams.db import open_db  # noqa: E402
from yaams.quality import message_context  # noqa: E402

EVAL = Path.home() / "brain/feed/eval"
SHEETS = ["jeff/owner_junk_labels_200_2026-10-02.tsv", "jeff/owner_junk_labels_a1_50_2026-09-25.tsv",
          "jeff/owner_junk_labels_test2_250_2026-10-04.tsv"]


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  ap.add_argument("--n", type=int, default=50)
  ap.add_argument("--out", default=str(EVAL / "jeff/context_keep_sheet.tsv"))
  a = ap.parse_args()
  out = Path(a.out)
  if out.exists():
    sys.exit(f"refusing to overwrite {out}")
  conn = open_db(a.db, readonly=True)
  relabel = {}
  for line in open(EVAL / "jeff/relabel-owner-v1/all/sonnet_verdicts_run1.jsonl"):
    relabel.update(json.loads(line))
  scope = set(load_ckpt(1)) & set(load_ckpt(2))
  excluded = set()
  for s in SHEETS:
    excluded |= {r["item_id"] for r in csv.DictReader(open(EVAL / s), delimiter="\t")}
  for i in list(excluded):  # neighbours of owner rows carry their text as context
    r = conn.execute("SELECT thread_id, timestamp FROM items WHERE id=?", (i,)).fetchone()
    if r is None:
      continue
    for op, order in (("<", "DESC"), (">", "ASC")):
      n = conn.execute(f"SELECT id FROM items WHERE thread_id=? AND timestamp{op}? ORDER BY timestamp {order} LIMIT 1",
                       (r[0], r[1])).fetchone()
      if n:
        excluded.add(n[0])
  cands = []
  for i, v in relabel.items():
    if v != "KEEP" or i not in scope or i in excluded:
      continue
    row = conn.execute("SELECT id, source, thread_id, timestamp, content FROM items WHERE id=?", (i,)).fetchone()
    if row is None:
      continue
    row = dict(row)
    prev, nxt = message_context(conn, row)
    if "?" in prev:
      cands.append((row, prev, nxt))
  print(f"relabel KEEP rows replying to a question: {len(cands)}")
  pick = random.Random(20261004).sample(cands, min(a.n, len(cands)))
  flat = lambda s: " / ".join(ln.strip() for ln in (s or "").strip().splitlines() if ln.strip())  # noqa: E731
  with open(out, "w", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["n", "source", "prev", "target", "next", "owner_verdict (J/K)", "item_id"])
    for n, (r, p, nx) in enumerate(pick, 1):
      w.writerow([n, r["source"], flat(p), flat(r["content"]), flat(nx), "", r["id"]])
  print(f"wrote {len(pick)} rows -> {out}")


if __name__ == "__main__":
  main()
