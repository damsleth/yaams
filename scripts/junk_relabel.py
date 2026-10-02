"""Relabel the A1 junk scope with a new rubric (v2 = the owner's bar), read-only on a db copy.

  .venv/bin/python scripts/junk_relabel.py --db <copy> --prompt scripts/junk_verdict_prompt.owner.md \
      --out ~/brain/feed/eval/jeff/relabel-owner --judge "copilot -s ..." [--ids tune|test|all]

Reuses junk_sonnet_pass (render, judge, resumable per-batch checkpoints) on the
A1 ids (both Sonnet runs judged the row), not `junk_reason IS NULL`: the old
consensus is applied, so that filter drops the JUNK class. `--ids tune|test`
restricts to the owner's 200-row sheet halves (n 1-100 tune, 101-200 test) and
prints agreement with the owner.
"""
import argparse
import csv
import os
import sys
from pathlib import Path


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  ap.add_argument("--prompt", required=True)
  ap.add_argument("--out", required=True)
  ap.add_argument("--judge", required=True)
  ap.add_argument("--ids", choices=["all", "tune", "test"], default="all")
  ap.add_argument("--workers", type=int, default=6)
  a = ap.parse_args()
  os.environ["YAAMS_JUNK_PROMPT"] = a.prompt  # read at import by junk_sonnet_pass
  os.environ["YAAMS_JUNK_PASS_DIR"] = str(Path(a.out).expanduser() / a.ids)
  sys.path.insert(0, str(Path(__file__).parent))
  import junk_sonnet_pass as jsp

  from yaams.db import open_db

  sheet = list(csv.DictReader(open(Path.home() / "brain/feed/eval/jeff/ft/junk-v1/sample_sheet.tsv"),
                              delimiter="\t"))
  owner = {r["item_id"]: (int(r["n"]), r["owner_verdict (J/K)"]) for r in sheet}
  out_dir = jsp.CKPT_DIR  # set from YAAMS_JUNK_PASS_DIR at import
  if a.ids == "all":
    jsp.CKPT_DIR = str(Path.home() / "brain/feed/eval/junk-pass")  # the original Sonnet runs
    ids = sorted(set(jsp.load_ckpt(1)) & set(jsp.load_ckpt(2)))
  else:
    lo, hi = (1, 100) if a.ids == "tune" else (101, 200)
    ids = sorted(i for i, (n, _) in owner.items() if lo <= n <= hi)
  jsp.CKPT_DIR = out_dir
  conn = open_db(a.db, readonly=True)
  rows = []
  for s in range(0, len(ids), 500):
    ch = ids[s:s + 500]
    q = f"SELECT id, source, thread_id, timestamp, content FROM items WHERE id IN ({','.join('?' * len(ch))})"
    rows += [dict(r) for r in conn.execute(q, ch)]
  rows.sort(key=lambda r: (r["thread_id"] or "", r["timestamp"]))
  print(f"{len(rows)} rows -> {jsp.CKPT_DIR}", flush=True)
  try:
    v = jsp.run_pass(conn, rows, a.workers, run_id=1, judge=a.judge)
  except jsp.RateLimited as e:
    print(f"STOPPED: {e}")
    return
  junk = sum(v.get(r["id"]) == "JUNK" for r in rows)
  print(f"JUNK {junk}/{len(rows)} ({junk / len(rows):.1%})")
  lab = [(v[i], owner[i][1]) for i in v if i in owner and owner[i][1]]
  if lab:
    agree = sum((x == "JUNK") == (o == "J") for x, o in lab)
    jr = [x for x, o in lab if o == "J"]
    kr = [x for x, o in lab if o == "K"]
    print(f"vs owner: agree {agree}/{len(lab)}; owner-J called JUNK {sum(x == 'JUNK' for x in jr)}/{len(jr)}; "
          f"owner-K kept {sum(x == 'KEEP' for x in kr)}/{len(kr)}")


if __name__ == "__main__":
  main()
