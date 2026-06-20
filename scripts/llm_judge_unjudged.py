#!/usr/bin/env python3
"""LLM-judge queries to densify the gold set. Two lanes:

* default — judge the *unjudged* answer-shaped queries (the original jun19 lane,
  now largely drained: 1 query left).
* ``--rejudge-misses`` — re-grade queries whose latest verdict is ``miss``
  against *current* retrieval. A miss was recorded when the right doc wasn't
  surfaced, but retrieval has improved since (entity boost, narrow-date de-boost,
  rrf/k tuning). If the LLM now finds a correct doc we append a hit/correction
  row; since the harness's _load_gold takes MAX(id) per query, the new row
  supersedes the old miss and converts it into a scorable gold. This is the main
  untapped densification lever (~46 re-judgeable misses).

Replay each query, show the LLM the ranked results, let it name the correct rank
(or none), write a hit/correction/miss row. Every written row stamps its
provenance into ``payload`` (``llm-judge`` / ``llm-rejudge``) so the gold set is
auditable by source, not just by id range. Dry-run unless ``--apply``. Additive
only — never overwrites a row. Reverse with:
    DELETE FROM query_feedback WHERE id >= <first_new_id>;
or, by provenance:
    DELETE FROM query_feedback WHERE payload LIKE 'llm-rejudge%';

Usage: .venv/bin/python scripts/llm_judge_unjudged.py [--apply] [--limit N]
       .venv/bin/python scripts/llm_judge_unjudged.py --rejudge-misses [--apply]
       .venv/bin/python scripts/llm_judge_unjudged.py --rejudge-misses --db ~/brain/autoresearch_fixture.db  # safe dry preview
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


_VERIFY_VOTES = 3  # best-of-N majority to tame the soft/nondeterministic verdict


def _verify_correct(llm, prompt: str, votes: int = _VERIFY_VOTES) -> bool:
  """Strict adversarial check, best-of-N. Returns True only if a majority of
  votes say the (query, doc) pair is correct. Each call samples independently,
  so a label that flip-flops between runs fails the majority and is rejected —
  precision over recall, by design. Any vote that errors counts as 'not correct'."""
  yes = 0
  need = votes // 2 + 1
  for _ in range(votes):
    try:
      out = llm.complete(prompt, max_tokens=30).text
      m = re.search(r"\{.*\}", out, re.DOTALL)
      if m and json.loads(m.group(0)).get("correct") is True:
        yes += 1
    except Exception:  # noqa: BLE001 — a failed vote is a 'no'
      pass
    if yes >= need:  # early-out: majority reached
      return True
  return yes >= need

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

# Adversarial second pass — the first-pass verdicts are soft (a vague one-word
# query like "kunde" happily "matches" any chatty message). This strict check
# defaults to rejection so only a clearly-correct (query, doc) pair becomes gold.
VERIFY_PROMPT = """Does this specific search result DIRECTLY and CORRECTLY answer the query? \
Be strict: answer "no" if the result is only tangentially related, is a vague \
or partial match, requires guessing, or if you are unsure.

Query: {query}

Candidate result:
[{kind}] {subject} :: {content}

Reply with ONLY one line of JSON: {{"correct": true|false}}
"""


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--apply", action="store_true", help="write rows (else dry-run)")
  ap.add_argument("--limit", type=int, default=0)
  ap.add_argument("--top-k", type=int, default=8)
  ap.add_argument("--rejudge-misses", action="store_true",
                  help="re-grade queries whose latest verdict is miss against current retrieval")
  ap.add_argument("--db", default=None, help="DB override (e.g. the fixture for a safe dry preview)")
  ap.add_argument("--no-verify", action="store_true",
                  help="skip the strict adversarial second pass (faster, noisier labels)")
  ap.add_argument("--votes", type=int, default=_VERIFY_VOTES,
                  help="best-of-N majority for the verify pass (default 3; 1 = single check)")
  args = ap.parse_args()
  prov = "llm-rejudge" if args.rejudge_misses else "llm-judge"

  cfg = load_config()
  rc = cfg.get("retrieve")
  syn = normalize_synonym_groups(rc.get("synonyms") if isinstance(rc, dict) else None)
  self_ids = _self_identities(cfg)
  emb = Embedder(**_embed_config(cfg), quiet=True)
  llm = llm_adapter_from_config(cfg)
  db_path = args.db or str(get_db_path(cfg))
  conn = open_db(db_path, readonly=not args.apply)

  if args.rejudge_misses:
    # Queries whose *latest* verdict is a miss and that returned candidates the
    # LLM can grade. A new hit/correction row supersedes the miss (harness uses
    # MAX(id)), converting it into a scorable gold.
    rows = conn.execute(
      """
      SELECT q.id, q.text, q.top_k, q.source_filter, q.since, q.until, q.parsed_query, q.shape
      FROM queries q
      JOIN (SELECT query_id, MAX(id) AS mid FROM query_feedback GROUP BY query_id) last
        ON last.query_id = q.id
      JOIN query_feedback f ON f.id = last.mid
      WHERE f.kind = 'miss' AND COALESCE(q.results_returned,0) > 0
      ORDER BY q.id
      """
    ).fetchall()
  else:
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

  counts = {"hit": 0, "correction": 0, "miss": 0, "skip": 0, "rejected": 0}
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
    counts[kind] += 1
    if args.rejudge_misses and kind == "miss":
      # Still a miss after re-judging — leave the existing miss row untouched.
      continue
    result_id = res[rank - 1].id if kind != "miss" and 1 <= (rank or 0) <= len(res) else None

    # Adversarial verification (on by default; --no-verify to skip): a strict
    # second pass that defaults to rejection, so soft first-pass picks (vague
    # one-word queries "matching" any chatty message) don't become false gold.
    if not args.no_verify and result_id is not None:
      cand = res[rank - 1]
      vprompt = VERIFY_PROMPT.format(
        query=text, kind=cand.kind,
        subject=cand.subject[:80], content=(cand.content or "")[:300],
      )
      if not _verify_correct(llm, vprompt, votes=args.votes):
        counts[kind] -= 1
        counts["rejected"] += 1
        print(f"  [reject    ] rank={rank} {text[:55]!r} (failed best-of-{args.votes} verify)")
        continue

    # Stamp provenance into payload so every LLM-written row is auditable by
    # source (not just by id range, which the original tool relied on).
    detail = f"rank {rank} is correct" if kind == "correction" else f"rank {rank}"
    payload = f"{prov}: {detail}" if kind != "miss" else prov
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
