#!/usr/bin/env python3
"""LLM-judge the unjudged answer-shaped queries to densify the gold set.

Same provenance as the existing LLM-judged feedback (ids 99-163): replay each
query, show the LLM the ranked results, let it name the correct rank (or none),
write a hit/correction/miss row. Dry-run unless --apply. Additive only — never
touches an already-judged query, never overwrites. Reverse with:
    DELETE FROM query_feedback WHERE id >= <first_new_id>;

Usage: .venv/bin/python scripts/llm_judge_unjudged.py [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from autoresearch_retrieval import _parsed_from_json  # type: ignore

from yaams.cli._shared import _embed_config, _self_identities
from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.enrich import Embedder
from yaams.retrieve import HybridQueryConfig, filter_results_by_entities, route
from yaams.retrieve import query as run_query
from yaams.retrieve.synonyms import normalize_synonym_groups
from yaams.synthesize.llm import llm_adapter_from_config
from yaams.time import parse_iso_datetime

from datetime import UTC, datetime  # noqa: E402


def now_iso() -> str:
  return datetime.now(UTC).isoformat()

PROMPT = """You are grading a personal-memory search result. The user's query and the \
top ranked results are below. Decide which single result (if any) best and \
correctly answers the query.

Query: {query}

Results:
{results}

Reply with ONLY one line of JSON, no prose:
{{"verdict": "hit"|"correction"|"miss", "rank": <int or null>}}
- "hit": rank 1 is the correct answer -> rank 1
- "correction": a correct answer exists but is NOT at rank 1 -> its rank
- "miss": no result correctly answers the query -> rank null
"""


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--apply", action="store_true", help="write rows (else dry-run)")
  ap.add_argument("--limit", type=int, default=0)
  ap.add_argument("--top-k", type=int, default=8)
  args = ap.parse_args()

  cfg = load_config()
  rc = cfg.get("retrieve")
  syn = normalize_synonym_groups(rc.get("synonyms") if isinstance(rc, dict) else None)
  self_ids = _self_identities(cfg)
  emb = Embedder(**_embed_config(cfg), quiet=True)
  llm = llm_adapter_from_config(cfg)
  db_path = str(get_db_path(cfg))
  conn = open_db(db_path, readonly=not args.apply)

  rows = conn.execute(
    """
    SELECT q.id, q.text, q.top_k, q.source_filter, q.since, q.until, q.parsed_query, q.shape
    FROM queries q
    WHERE NOT EXISTS (SELECT 1 FROM query_feedback f WHERE f.query_id=q.id)
      AND q.shape IN ('factual','synthesis','event_anchored')
      AND COALESCE(q.results_returned,0) > 0
    ORDER BY q.id
    """
  ).fetchall()
  if args.limit:
    rows = rows[: args.limit]

  counts = {"hit": 0, "correction": 0, "miss": 0, "skip": 0}
  for r in rows:
    text = r["text"]
    parsed = _parsed_from_json(r["parsed_query"], text)
    sf = json.loads(r["source_filter"] or "[]") or None
    base = HybridQueryConfig(
      top_k=max(r["top_k"] or 10, args.top_k),
      source_filter=sf,
      since=parse_iso_datetime(r["since"]) if r["since"] else None,
      until=parse_iso_datetime(r["until"]) if r["until"] else None,
      synonym_groups=syn,
    )
    qcfg = route(parsed, base, self_identities=self_ids) if parsed else base
    fts_text = " ".join(parsed.topic_terms) if parsed and parsed.topic_terms else text
    res = run_query(conn, fts_text, embedding=emb.embed_batch([text])[0], config=qcfg)
    if parsed and qcfg.entity_filter:
      res = filter_results_by_entities(res, conn, qcfg.entity_filter)
    res = res[: args.top_k]
    if not res:
      counts["skip"] += 1
      continue

    listing = "\n".join(
      f"{i}. [{x.kind}] {x.subject[:60]} :: {(x.content or '')[:160]}"
      for i, x in enumerate(res, 1)
    )
    try:
      out = llm.complete(PROMPT.format(query=text, results=listing), max_tokens=120).text
      m = re.search(r"\{.*\}", out, re.DOTALL)
      verdict = json.loads(m.group(0)) if m else {}
      kind = verdict.get("verdict")
      rank = verdict.get("rank")
    except Exception as exc:  # noqa: BLE001
      print(f"  ! {text[:50]!r}: judge error {exc}")
      counts["skip"] += 1
      continue

    if kind == "hit":
      rank = 1
    if kind not in ("hit", "correction", "miss") or (kind != "miss" and not rank):
      counts["skip"] += 1
      continue
    result_id = res[rank - 1].id if kind != "miss" and 1 <= (rank or 0) <= len(res) else None
    counts[kind] += 1
    payload = f"rank {rank} is correct" if kind == "correction" else None
    print(f"  [{kind:10}] rank={rank} {text[:55]!r}")
    if args.apply:
      conn.execute(
        "INSERT INTO query_feedback (query_id, kind, result_id, payload, ts) VALUES (?,?,?,?,?)",
        (r["id"], kind, result_id, payload, now_iso()),
      )

  if args.apply:
    conn.commit()
  conn.close()
  print(f"\n{counts}  ({'APPLIED' if args.apply else 'DRY-RUN'})")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
