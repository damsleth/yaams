"""Re-parse gold queries whose stored parse is a silent fallback (no entities), as of when
they were asked, and write a harness parse override (P6: the harness otherwise replays the
degraded parse forever).

  .venv/bin/python scripts/reparse_fallback_golds.py --db <fixture copy> --out overrides.json
  .venv/bin/python scripts/autoresearch_retrieval.py --db <fixture copy> --parse-override overrides.json ...
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from autoresearch_retrieval import _load_gold  # noqa: E402

from yaams.config import load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402
from yaams.retrieve.parse import parse_query  # noqa: E402
from yaams.synthesize.llm import llm_adapter_from_config  # noqa: E402
from yaams.time import parse_iso_datetime  # noqa: E402


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  ap.add_argument("--out", required=True)
  a = ap.parse_args()
  conn = open_db(a.db, readonly=True)
  llm = llm_adapter_from_config(load_config())
  out = {}
  for g in _load_gold(conn)[0]:
    stored = json.loads(g["parsed_query"] or "{}")
    if not stored.get("fallback_used"):
      continue
    fresh = parse_query(g["text"], llm, conn, now=parse_iso_datetime(g["ts"]) if g["ts"] else None)
    d = json.loads(fresh.to_json())
    out[g["query_id"]] = d
    print(f"{g['text'][:50]!r}: fallback={d.get('fallback_used')} shape={d.get('shape')} "
          f"entities={d.get('entities')} dates={d.get('date_range')}")
  Path(a.out).write_text(json.dumps(out, indent=1, default=str))
  print(f"{len(out)} overrides -> {a.out}")


if __name__ == "__main__":
  main()
