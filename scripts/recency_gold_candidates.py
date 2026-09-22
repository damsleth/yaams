#!/usr/bin/env python3
"""Candidate sheet for a recency-sensitive gold slice. Read-only.

Replays the owner's own short queries from the live query log through the
retrieval engine with the recency lane on, and lists the recent hits so the
owner can mark the one they actually wanted (`.plans/eval-gold-hygiene.md`,
task 2). Nothing here writes to the db: it is opened `mode=ro` and
`yaams.retrieve.hybrid.query` never logs.

Query selection. `.plans/done/recency-lane.md` reports "79 genuine short
queries from the query log (all predating the session that built this)" but
neither the plan nor git history records the exact filter. This script uses:

  * `queries.provenance = 'cli'` (the owner typing, not tests/mcp/legacy),
  * at most `--max-tokens` whitespace tokens (default 3),
  * `ts` before `--before` (default 2026-09-16, the lane session),
  * deduped by lowercased, stripped text, keeping the earliest `ts`.

That yields 113 queries on the live db as of 2026-09-22, not 79; the two
sets overlap but are not the same population, and the difference is stated
here rather than tuned away.

Replay. FTS-only (`embedding=None`, so the embedding model never loads),
`recency_lane_days` (default 60), `recency_lane_weight` 1.0 as in the
recorded measurement, and `recency_now` pinned to the query's own `ts` so
"recent" means recent when the owner asked. The general lanes are not time
scoped; results dated after the query, or older than the window, are dropped
afterwards. The first `--top` (default 5) survivors are written per query.

Output TSV columns: `query, rank, item_id, ts, snippet, query_id, query_ts`.
`rank` is the position in the fused result list. The trailing `query_id` is
what task 4 needs to file the marks as `query_feedback` rows; text alone is
ambiguous after dedup.

    /Users/damsleth/code/yaams/.venv/bin/python scripts/recency_gold_candidates.py \\
        [--db ~/brain/feed/data.db] [--out ~/brain/feed/eval/recency_candidates.tsv]
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
  sys.path.insert(0, str(_REPO))

from yaams.db import open_db  # noqa: E402
from yaams.retrieve.hybrid import HybridQueryConfig  # noqa: E402
from yaams.retrieve.hybrid import query as run_query  # noqa: E402
from yaams.time import ensure_utc, parse_iso_datetime  # noqa: E402

_DEFAULT_DB = Path.home() / "brain" / "feed" / "data.db"
_DEFAULT_OUT = Path.home() / "brain" / "feed" / "eval" / "recency_candidates.tsv"
_COLUMNS = ("query", "rank", "item_id", "ts", "snippet", "query_id", "query_ts")


def select_queries(conn, before: str, max_tokens: int) -> list[dict]:
  """Owner-typed short queries, deduped by lowercased text, earliest ts kept."""
  rows = conn.execute(
    "SELECT id, text, ts FROM queries WHERE provenance = 'cli' AND ts < ? ORDER BY ts",
    (before,),
  ).fetchall()
  seen: set[str] = set()
  out: list[dict] = []
  for r in rows:
    text = (r["text"] or "").strip()
    key = text.lower()
    if not text or len(text.split()) > max_tokens or key in seen:
      continue
    seen.add(key)
    out.append({"id": r["id"], "text": text, "ts": ensure_utc(parse_iso_datetime(r["ts"]))})
  return out


def _snippet(subject: str, content: str, width: int = 120) -> str:
  text = " ".join(f"{subject or ''} {content or ''}".split())
  return text[:width]


def recent_candidates(conn, q: dict, days: float, top: int) -> list[tuple]:
  """Top `top` fused hits dated within `days` before the query's own ts."""
  cfg = HybridQueryConfig(
    top_k=50,
    recency_lane_days=days,
    recency_now=q["ts"],
  )
  results = run_query(conn, q["text"], embedding=None, config=cfg)
  horizon = q["ts"] - timedelta(days=days)
  out: list[tuple] = []
  for rank, res in enumerate(results, 1):
    ts = ensure_utc(res.timestamp)
    if horizon <= ts <= q["ts"]:
      out.append((rank, res.id, ts, _snippet(res.subject, res.content)))
      if len(out) >= top:
        break
  return out


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--db", default=str(_DEFAULT_DB))
  ap.add_argument("--out", default=str(_DEFAULT_OUT))
  ap.add_argument("--days", type=float, default=60.0)
  ap.add_argument("--top", type=int, default=5)
  ap.add_argument("--max-tokens", type=int, default=3)
  ap.add_argument("--before", default="2026-09-16")
  args = ap.parse_args()

  conn = open_db(args.db, readonly=True)
  queries = select_queries(conn, args.before, args.max_tokens)
  n_rows = 0
  n_empty = 0
  lines = ["\t".join(_COLUMNS)]
  for q in queries:
    hits = recent_candidates(conn, q, args.days, args.top)
    if not hits:
      n_empty += 1
    for rank, item_id, ts, snippet in hits:
      lines.append("\t".join((
        q["text"].replace("\t", " "), str(rank), item_id, ts.isoformat(), snippet,
        q["id"], q["ts"].isoformat(),
      )))
      n_rows += 1
  conn.close()

  out = Path(args.out).expanduser()
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text("\n".join(lines) + "\n")
  print(f"queries: {len(queries)}  rows: {n_rows}  queries_without_recent_hit: {n_empty}")
  print(f"wrote {out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
