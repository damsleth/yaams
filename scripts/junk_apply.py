"""Apply owner-bar junk verdicts to the live db as `llm:junk-owner`, reversibly.

  .venv/bin/python scripts/junk_apply.py VERDICTS.jsonl [...]            # dry run: counts only
  .venv/bin/python scripts/junk_apply.py VERDICTS.jsonl [...] --apply    # write + undo log
  .venv/bin/python scripts/junk_apply.py --undo ~/brain/feed/eval/jeff/apply/<ts>.jsonl

VERDICTS are `{item_id: "JUNK"|"KEEP"}` jsonl lines (junk_relabel.py checkpoints,
jeff_junk_label.py output). Precedence, highest first:
  1. protected: an item that was ever a hit/correction answer in query_feedback is
     never hidden (a junk gold silently inverts the exclude_junk gate);
  2. the owner's own labels (the 200-row blind sheet and the A1 50-row sheet);
  3. the verdict files, later files overriding earlier ones.
JUNK sets `llm:junk-owner` on rows whose junk_reason is NULL. KEEP clears an old
`llm:junk` (the two-run Sonnet consensus, judged at the old, looser bar).
`mech:*` rows are never touched. Every change is logged before it is committed,
`--undo` restores exactly those rows (only where the value is still ours).
"""
import argparse
import csv
import json
import os
import time
from pathlib import Path

import yaml

from yaams.db import open_db

EVAL = Path.home() / "brain/feed/eval"
OWNER_SHEETS = [(EVAL / "jeff/owner_junk_labels_200_2026-10-02.tsv", "owner_verdict (J/K)", "J"),
                (EVAL / "jeff/owner_junk_labels_a1_50_2026-09-25.tsv", "owner_verdict (JUNK/KEEP)", "JUNK")]
NEW = "llm:junk-owner"
OLD = "llm:junk"


def db_path():
  cfg = yaml.safe_load(open(Path.home() / ".config/yaams/config.yaml"))
  return os.path.expanduser(cfg.get("db_path") or "~/brain/feed/data.db")


def chunks(xs, n=500):
  for i in range(0, len(xs), n):
    yield xs[i:i + n]


def undo(conn, log):
  rows = [json.loads(line) for line in open(log)]
  n = 0
  for ch in chunks(rows):
    with conn:
      for r in ch:
        n += conn.execute("UPDATE items SET junk_reason=? WHERE id=? AND junk_reason IS ?",
                          (r["before"], r["id"], r["after"])).rowcount
  print(f"restored {n}/{len(rows)} rows from {log}")


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("verdicts", nargs="*")
  ap.add_argument("--apply", action="store_true")
  ap.add_argument("--undo")
  ap.add_argument("--db", default=None, help="default: db_path from ~/.config/yaams/config.yaml")
  a = ap.parse_args()
  conn = open_db(a.db or db_path(), readonly=not (a.apply or a.undo))
  if a.undo:
    return undo(conn, a.undo)

  verdict = {}
  for p in a.verdicts:
    for line in open(Path(p).expanduser()):
      verdict.update(json.loads(line))
  owned = 0
  for path, col, junk in OWNER_SHEETS:
    for r in csv.DictReader(open(path), delimiter="\t"):
      if r[col]:
        verdict[r["item_id"]] = "JUNK" if r[col] == junk else "KEEP"
        owned += 1
  protected = {r[0] for r in conn.execute(
    "SELECT DISTINCT result_id FROM query_feedback WHERE kind IN ('hit', 'correction') AND result_id IS NOT NULL")}

  current = {}
  ids = list(verdict)
  for ch in chunks(ids):
    q = f"SELECT id, junk_reason FROM items WHERE id IN ({','.join('?' * len(ch))})"
    current.update(conn.execute(q, ch).fetchall())
  plan, skipped = [], {"missing": 0, "protected": 0, "mech": 0}
  for i, v in verdict.items():
    if i not in current:
      skipped["missing"] += 1
      continue
    before = current[i]
    if before and before.startswith("mech:"):
      skipped["mech"] += 1
      continue
    if v == "JUNK" and before is None:
      if i in protected:
        skipped["protected"] += 1
        continue
      plan.append({"id": i, "before": None, "after": NEW})
    elif v == "KEEP" and before == OLD:
      plan.append({"id": i, "before": OLD, "after": None})
  hide = sum(p["after"] == NEW for p in plan)
  unhide = len(plan) - hide
  already = sum(1 for i, v in verdict.items() if v == "JUNK" and current.get(i) in (OLD, NEW))
  print(f"verdicts {len(verdict)} ({owned} owner-labelled); protected gold items {len(protected)}")
  print(f"plan: hide {hide} as {NEW}, un-hide {unhide} old {OLD} judged KEEP; already hidden {already}; "
        f"skipped {skipped}")
  if not a.apply:
    print("dry run: nothing written (pass --apply)")
    return
  log_dir = EVAL / "jeff/apply"
  log_dir.mkdir(parents=True, exist_ok=True)
  log = log_dir / f"{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
  with open(log, "w") as f:
    for p in plan:
      f.write(json.dumps(p) + "\n")
  n = 0
  for ch in chunks(plan):
    with conn:  # short transactions: other agents share the live db
      for p in ch:
        n += conn.execute("UPDATE items SET junk_reason=? WHERE id=? AND junk_reason IS ?",
                          (p["after"], p["id"], p["before"])).rowcount
  print(f"applied {n}/{len(plan)}; undo with: scripts/junk_apply.py --undo {log}")


if __name__ == "__main__":
  main()
