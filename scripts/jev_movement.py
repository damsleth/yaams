"""Per-gold rank movement of a harness run vs an anchor (P1 guard for Jev B plans).

  .venv/bin/python scripts/jev_movement.py --anchor dev:hybrid+nojunk --db <fixture> RANKS.json [...]

RANKS.json comes from `autoresearch_retrieval.py --ranks-out`. The anchor is a
`{split}:{mode}` key in scripts/.autoresearch_state.json, or a ranks JSON path.
Query language per gold via yaams.enrich.entities.detect_lang.
"""
import argparse
import json
from pathlib import Path

from yaams.db import open_db
from yaams.enrich.entities import detect_lang

STATE = Path(__file__).parent / ".autoresearch_state.json"
BIG = 10**6  # a miss (None) ranks below everything


def load_anchor(spec):
  p = Path(spec)
  if p.exists():
    return json.loads(p.read_text())
  return json.loads(STATE.read_text())[spec]["ranks"]


def movement(anchor, ranks, lang):
  rows = {}
  for group in ("all", "en", "nb"):
    qs = [q for q in anchor if group == "all" or lang.get(q) == group]
    a = {q: anchor[q] or BIG for q in qs}
    b = {q: ranks.get(q) or BIG for q in qs}
    rows[group] = {
      "n": len(qs), "up": sum(b[q] < a[q] for q in qs), "down": sum(b[q] > a[q] for q in qs),
      "ge5_to_top3": sum(a[q] >= 5 and b[q] <= 3 for q in qs),
      "rank1_regressions": sum(a[q] == 1 and b[q] != 1 for q in qs),
      "new_rank1": sum(a[q] != 1 and b[q] == 1 for q in qs),
      "adjacent_swaps": sum(abs(a[q] - b[q]) == 1 for q in qs if a[q] < BIG and b[q] < BIG),
    }
  return rows


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--anchor", default="dev:hybrid+nojunk")
  ap.add_argument("--db", required=True)
  ap.add_argument("ranks", nargs="+")
  a = ap.parse_args()
  anchor = load_anchor(a.anchor)
  conn = open_db(a.db, readonly=True)
  ph = ",".join("?" * len(anchor))
  lang = {q: "nb" if detect_lang(t) == "no" else "en"
          for q, t in conn.execute(f"SELECT id, text FROM queries WHERE id IN ({ph})", list(anchor))}
  print("run\tgroup\tn\tup\tdown\t>=5->top3\trank1_regr\tnew_rank1\tadjacent")
  for path in a.ranks:
    mv = movement(anchor, json.loads(Path(path).read_text()), lang)
    for g, r in mv.items():
      print(f"{Path(path).stem}\t{g}\t" + "\t".join(str(v) for v in r.values()))


if __name__ == "__main__":
  main()
