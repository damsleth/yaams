"""Render the decision rows (200 dev labels + 250 test-2) as unlabelled model inputs.

  .venv/bin/python scripts/junk_decision_rows.py --out ~/brain/feed/eval/jeff/decision/rows.jsonl

Rows are {"id", "state", "question"} in the fine-tune's exact format (yaams.quality); no
labels are written, so scoring a model on them does not read test-2's outcome.
"""
import argparse
import csv
import json
from pathlib import Path

from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.quality import MODEL_CRITERION, MODEL_STATE, junk_block

EVAL = Path.home() / "brain/feed/eval/jeff"
SHEETS = [EVAL / "owner_junk_labels_200_2026-10-02.tsv", EVAL / "owner_junk_labels_test2_250_2026-10-04.tsv"]


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--out", required=True)
  a = ap.parse_args()
  conn = open_db(str(get_db_path(load_config())), readonly=True)
  ids = [r["item_id"] for s in SHEETS for r in csv.DictReader(open(s), delimiter="\t")]
  out = Path(a.out).expanduser()
  out.parent.mkdir(parents=True, exist_ok=True)
  n = 0
  with open(out, "w") as f:
    for i in ids:
      r = conn.execute("SELECT id, source, thread_id, timestamp, content FROM items WHERE id=?", (i,)).fetchone()
      if r is None:
        raise SystemExit(f"labelled item {i} missing from the live db")
      q = {"type": "noul", "instructions": junk_block(conn, dict(r)), "criteria": {"true": MODEL_CRITERION}}
      f.write(json.dumps({"id": i, "state": MODEL_STATE, "question": q}, ensure_ascii=False) + "\n")
      n += 1
  print(f"{n} rows -> {out}")


if __name__ == "__main__":
  main()
