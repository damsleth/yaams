"""Append-only signal logging for queries, retrieval results, and feedback."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Sequence

from yaams.retrieve import HybridResult


def new_query_id() -> str:
  return f"q_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(4)}"


def detect_provenance(explicit: str | None = None) -> str:
  """Return the provenance label for a logged query.

  Caller can pass ``explicit`` (e.g. ``"cli"``); otherwise
  detect a pytest run via the ``PYTEST_CURRENT_TEST`` env var that pytest
  exports during test execution. Falls back to ``"unknown"``.
  """
  if explicit:
    return explicit
  if os.environ.get("PYTEST_CURRENT_TEST"):
    return "test"
  return "unknown"


def log_query(
  conn: sqlite3.Connection,
  *,
  query_id: str,
  text: str,
  top_k: int,
  source_filter: list[str] | None,
  since: str | None,
  until: str | None,
  results: Sequence[HybridResult],
  cited_result_ids: Sequence[str] = (),
  answer: str | None = None,
  backend: str | None = None,
  model: str | None = None,
  latency_ms: float | None = None,
  retrieval_ms: float | None = None,
  synthesis_ms: float | None = None,
  parsed_query: str | None = None,
  shape: str | None = None,
  confidence: str | None = None,
  confidence_reason: str | None = None,
  gaps: Sequence[str] | None = None,
  parser_fallback: bool = False,
  provenance: str | None = None,
  ts: datetime | None = None,
) -> None:
  ts_iso = (ts or datetime.now(UTC)).isoformat()
  cited_set = set(cited_result_ids)
  gaps_text = (
    json.dumps(list(gaps), ensure_ascii=False) if gaps is not None else None
  )
  prov = detect_provenance(provenance)
  with conn:
    conn.execute(
      """
      INSERT INTO queries (
        id, text, top_k, source_filter, since, until,
        backend, model, latency_ms, retrieval_ms, synthesis_ms,
        results_returned, answer, ts,
        parsed_query, shape, confidence, confidence_reason, gaps,
        parser_fallback, provenance
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        query_id,
        text,
        top_k,
        json.dumps(source_filter or [], ensure_ascii=False),
        since,
        until,
        backend,
        model,
        latency_ms,
        retrieval_ms,
        synthesis_ms,
        len(results),
        answer,
        ts_iso,
        parsed_query,
        shape,
        confidence,
        confidence_reason,
        gaps_text,
        1 if parser_fallback else 0,
        prov,
      ),
    )
    for rank, result in enumerate(results, 1):
      conn.execute(
        """
        INSERT INTO query_results (
          query_id, rank, result_id, kind, source,
          rrf_score, fts_rank, vector_rank, cited
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          query_id,
          rank,
          result.id,
          result.kind,
          result.source,
          result.score,
          result.components.fts_rank,
          result.components.vector_rank,
          1 if result.id in cited_set else 0,
        ),
      )


def log_feedback(
  conn: sqlite3.Connection,
  *,
  query_id: str,
  kind: str,
  result_id: str | None = None,
  payload: str | dict | None = None,
  ts: datetime | None = None,
) -> int:
  ts_iso = (ts or datetime.now(UTC)).isoformat()
  payload_text: str | None
  if payload is None:
    payload_text = None
  elif isinstance(payload, str):
    payload_text = payload
  else:
    payload_text = json.dumps(payload, ensure_ascii=False)
  with conn:
    cursor = conn.execute(
      """
      INSERT INTO query_feedback (query_id, kind, result_id, payload, ts)
      VALUES (?, ?, ?, ?, ?)
      """,
      (query_id, kind, result_id, payload_text, ts_iso),
    )
  return int(cursor.lastrowid or 0)


# Feedback kinds that affirm a result vs. flag it as wrong. Used to derive
# validation / contradiction counts for trust verdicts (yaams.trust).
_VALIDATION_KINDS = ("hit", "relevant")
_CONTRADICTION_KINDS = ("correction",)


def feedback_counts(
  conn: sqlite3.Connection, result_ids: Sequence[str]
) -> dict[str, tuple[int, int]]:
  """Return ``{result_id: (validations, contradictions)}`` for *result_ids*.

  Validations are affirming feedback (hit / relevant); contradictions are
  corrections. Ids with no feedback are returned as ``(0, 0)``. Batched into a
  single GROUP BY scan over ``query_feedback``.
  """
  counts: dict[str, tuple[int, int]] = {rid: (0, 0) for rid in result_ids}
  if not counts:
    return counts
  placeholders = ",".join("?" for _ in counts)
  rows = conn.execute(
    f"""
    SELECT result_id, kind, COUNT(*) AS n
    FROM query_feedback
    WHERE result_id IN ({placeholders})
    GROUP BY result_id, kind
    """,
    tuple(counts),
  ).fetchall()
  for row in rows:
    rid, kind, n = row[0], row[1], int(row[2])
    valid, contra = counts.get(rid, (0, 0))
    if kind in _VALIDATION_KINDS:
      valid += n
    elif kind in _CONTRADICTION_KINDS:
      contra += n
    counts[rid] = (valid, contra)
  return counts


def recent_queries(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
  rows = conn.execute(
    """
    SELECT id, text, results_returned, latency_ms, backend, model, ts
    FROM queries
    ORDER BY ts DESC
    LIMIT ?
    """,
    (limit,),
  ).fetchall()
  return [dict(row) for row in rows]
