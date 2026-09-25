"""Jev A1: noul junk verdicts on the rows both Sonnet runs judged.

  .venv/bin/python scripts/jev_junk_pass.py --db <copy.db> --variant en,nb

Read-only. Scope is the 10-39 char messaging band (junk_sonnet_pass), but taken
as the id intersection of the two Sonnet runs, NOT `junk_reason IS NULL`: the
consensus was applied, so the NULL filter would drop every consensus-JUNK row.
Also scores the 37 owner junk-labelled gold items (`owner_gold: true`).
Writes ~/brain/feed/eval/jev/junk-<variant>.jsonl. Usage tag jev_a1_<variant>.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from junk_sonnet_pass import CKPT_DIR, context, load_ckpt  # noqa: E402

from yaams import jev  # noqa: E402
from yaams.db import open_db  # noqa: E402

HERE = Path(__file__).parent
RUBRICS = {"en": HERE / "junk_verdict_prompt.md", "nb": HERE / "junk_verdict_prompt.nb.md"}
CRITERION = "The TARGET message is junk: it carries no retrievable content on its own."
GOLD_SQL = Path.home() / "brain/feed/eval/junk_gold_labels.sql"


def rubric(variant):
  # the last paragraph is the "<n>: JUNK" output format for CLI judges; Jev answers a noul
  return RUBRICS[variant].read_text().strip().rsplit("\n\n", 1)[0]


def block(conn, row):
  p, n = context(conn, row)
  return f"[{row['source']}] prev: {p!r}\nTARGET: {row['content'].strip()!r}\nnext: {n!r}"


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True, help="a COPY of the live db")
  ap.add_argument("--variant", default="en,nb")
  ap.add_argument("--limit", type=int, default=0)
  ap.add_argument("--workers", type=int, default=8)
  a = ap.parse_args()

  conn = open_db(a.db, readonly=True)
  r1, r2 = load_ckpt(1), load_ckpt(2)
  ids = sorted(set(r1) & set(r2))
  gold_ids = sorted(set(re.findall(r"result_id='([0-9a-f]{64})'", GOLD_SQL.read_text())))
  print(f"sonnet runs from {CKPT_DIR}: {len(r1)} / {len(r2)}, both {len(ids)}; owner golds {len(gold_ids)}")
  if a.limit:
    ids = ids[: a.limit]
  rows = {}
  for chunk_start in range(0, len(ids + gold_ids), 500):
    chunk = (ids + gold_ids)[chunk_start:chunk_start + 500]
    q = f"SELECT id, source, thread_id, timestamp, content, lang FROM items WHERE id IN ({','.join('?' * len(chunk))})"
    rows.update({r["id"]: dict(r) for r in conn.execute(q, chunk)})
  gold = set(gold_ids)
  texts = {i: block(conn, rows[i]) for i in rows}
  print(f"rendered {len(texts)} rows ({len(set(ids) - set(rows))} scope ids missing from db)")

  for v in a.variant.split(","):
    state = {"task": rubric(v), "owner_context": "personal search index over Kim's messages"}
    stats = {}
    scores = jev.noul(state, texts, CRITERION, criterion_version=f"junk-1-{v}",
                      tag=f"jev_a1_{v}", workers=a.workers, stats=stats)
    out = jev.JEV_DIR / f"junk-{v}.jsonl"
    with open(out, "w") as f:
      for i, s in scores.items():
        r = rows[i]
        f.write(json.dumps({"item_id": i, "noul": s, "source": r["source"],
                            "lang": "nb" if r["lang"] == "no" else (r["lang"] or "other"),
                            "owner_gold": i in gold}) + "\n")
    print(f"{v}: {len(scores)}/{len(texts)} scored, {stats} -> {out}")


if __name__ == "__main__":
  main()
