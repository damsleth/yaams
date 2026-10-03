"""Build test-2: a blind random owner-label sheet of band rows no junk model was trained on.

  .venv/bin/python scripts/junk_test2_sheet.py [--n 250] [--out ~/brain/feed/eval/jeff/test2_sheet.tsv]

Population: 10-39 char messaging rows (the model band), not mech:*, outside the
A1 scope every junk model trained on (both Sonnet runs), not already
owner-labelled, and not a near-duplicate (normalized text) of any of those.
Order is shuffled; no model verdicts are shown. Read once, for one finalist
(.plans v3.1 decision rule).
"""
import argparse
import csv
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from junk_sonnet_pass import load_ckpt  # noqa: E402

from yaams.config import get_db_path, load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402
from yaams.quality import _MESSAGING_SOURCES_SQL, MODEL_BAND, message_context  # noqa: E402

EVAL = Path.home() / "brain/feed/eval"


def norm(s):
  return re.sub(r"\W+", " ", (s or "").casefold()).strip()


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--n", type=int, default=250)
  ap.add_argument("--out", default=str(EVAL / "jeff/test2_sheet.tsv"))
  a = ap.parse_args()
  out = Path(a.out).expanduser()
  if out.exists():
    sys.exit(f"refusing to overwrite {out} (it may hold owner labels)")
  conn = open_db(str(get_db_path(load_config())), readonly=True)
  trained = set(load_ckpt(1)) & set(load_ckpt(2))
  labelled = set()
  for f, col in ((EVAL / "jeff/owner_junk_labels_200_2026-10-02.tsv", "item_id"),
                 (EVAL / "jeff/owner_junk_labels_a1_50_2026-09-25.tsv", "item_id")):
    labelled |= {r[col] for r in csv.DictReader(open(f), delimiter="\t")}
  seen_text = set()
  for chunk in (list(trained | labelled)[i:i + 900] for i in range(0, len(trained | labelled), 900)):
    q = f"SELECT content FROM items WHERE id IN ({','.join('?' * len(chunk))})"
    seen_text |= {norm(c) for (c,) in conn.execute(q, chunk)}
  rows = [dict(r) for r in conn.execute(
    f"SELECT id, source, thread_id, timestamp, content FROM items WHERE {_MESSAGING_SOURCES_SQL} "
    "AND length(trim(content)) BETWEEN ? AND ? AND (junk_reason IS NULL OR junk_reason NOT LIKE 'mech:%')",
    MODEL_BAND)]
  pool, texts = [], set()
  for r in rows:
    t = norm(r["content"])
    if r["id"] in trained or r["id"] in labelled or t in seen_text or t in texts:
      continue
    texts.add(t)
    pool.append(r)
  print(f"band rows {len(rows)}; eligible (untrained, unlabelled, deduplicated) {len(pool)}")
  if len(pool) < a.n:
    sys.exit(f"only {len(pool)} eligible rows, need {a.n}")
  pick = random.Random(20261003).sample(pool, a.n)
  out.parent.mkdir(parents=True, exist_ok=True)
  flat = lambda s: " / ".join(ln.strip() for ln in (s or "").strip().splitlines() if ln.strip())  # noqa: E731
  with open(out, "w", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["n", "source", "prev", "target", "next", "owner_verdict (J/K)", "item_id"])
    for n, r in enumerate(pick, 1):
      p, nx = message_context(conn, r)
      w.writerow([n, r["source"], flat(p), flat(r["content"]), flat(nx), "", r["id"]])
  print(f"wrote {a.n} rows -> {out}")


if __name__ == "__main__":
  main()
