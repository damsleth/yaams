from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from yaams.retrieve import HybridResult, ScoreComponents
from yaams.schema import init_schema
from yaams.signals import log_feedback, log_query, new_query_id, recent_queries


def _open():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _result(rid: str, kind: str = "item") -> HybridResult:
  return HybridResult(
    id=rid,
    kind=kind,
    source="imessage",
    timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    subject="",
    content="hello",
    thread_id="t1",
    score=0.5,
    components=ScoreComponents(fts_rank=0, fts_score=-1.2, vector_rank=2, vector_distance=0.4, rrf_score=0.5),
  )


def test_new_query_id_unique_and_prefixed():
  a = new_query_id()
  b = new_query_id()
  assert a != b
  assert a.startswith("q_")


def test_log_query_persists_query_and_results():
  conn = _open()
  qid = "q_test"
  results = [_result("ra"), _result("rb", kind="consolidation")]
  log_query(
    conn,
    query_id=qid,
    text="what happened",
    top_k=10,
    source_filter=["imessage"],
    since=None,
    until=None,
    results=results,
    cited_result_ids=["ra"],
    answer="some answer",
    backend="dummy",
    model="test",
    latency_ms=42.0,
    retrieval_ms=20.0,
    synthesis_ms=22.0,
  )

  row = conn.execute("SELECT * FROM queries WHERE id = ?", (qid,)).fetchone()
  assert row is not None
  assert row["text"] == "what happened"
  assert row["results_returned"] == 2
  assert row["answer"] == "some answer"
  assert row["backend"] == "dummy"
  assert row["latency_ms"] == 42.0

  rows = conn.execute(
    "SELECT rank, result_id, kind, cited FROM query_results WHERE query_id = ? ORDER BY rank",
    (qid,),
  ).fetchall()
  assert [row["result_id"] for row in rows] == ["ra", "rb"]
  assert [row["kind"] for row in rows] == ["item", "consolidation"]
  assert rows[0]["cited"] == 1
  assert rows[1]["cited"] == 0


def test_log_feedback_inserts_row():
  conn = _open()
  qid = "q_test"
  log_query(
    conn,
    query_id=qid,
    text="x",
    top_k=1,
    source_filter=None,
    since=None,
    until=None,
    results=[],
  )

  fid = log_feedback(conn, query_id=qid, kind="hit", result_id="ra")
  assert fid > 0

  row = conn.execute("SELECT * FROM query_feedback WHERE id = ?", (fid,)).fetchone()
  assert row["query_id"] == qid
  assert row["kind"] == "hit"
  assert row["result_id"] == "ra"


def test_log_feedback_serializes_dict_payload():
  conn = _open()
  log_query(conn, query_id="q1", text="x", top_k=1, source_filter=None, since=None, until=None, results=[])
  log_feedback(conn, query_id="q1", kind="correction", payload={"expected": "Alice"})
  row = conn.execute("SELECT payload FROM query_feedback").fetchone()
  assert "Alice" in row["payload"]


def test_log_query_persists_structured_phase_h_fields():
  conn = _open()
  qid = "q_h"
  log_query(
    conn,
    query_id=qid,
    text="when did I first hear about ATLAS",
    top_k=10,
    source_filter=None,
    since=None,
    until=None,
    results=[_result("ra")],
    cited_result_ids=["ra"],
    answer="full LLM text",
    backend="dummy",
    model="test",
    parsed_query='{"shape":"first_occurrence"}',
    shape="first_occurrence",
    confidence="high",
    confidence_reason="strong evidence",
    gaps=["nothing about price"],
    parser_fallback=False,
  )
  row = conn.execute("SELECT * FROM queries WHERE id = ?", (qid,)).fetchone()
  assert row["shape"] == "first_occurrence"
  assert row["confidence"] == "high"
  assert row["confidence_reason"] == "strong evidence"
  assert "nothing about price" in row["gaps"]
  assert row["parser_fallback"] == 0
  assert "first_occurrence" in row["parsed_query"]


def test_log_query_default_parser_fallback_is_zero():
  conn = _open()
  log_query(
    conn,
    query_id="q_def",
    text="x",
    top_k=1,
    source_filter=None,
    since=None,
    until=None,
    results=[],
  )
  row = conn.execute("SELECT parser_fallback FROM queries WHERE id = ?", ("q_def",)).fetchone()
  assert row["parser_fallback"] == 0


def test_recent_queries_returns_most_recent_first():
  conn = _open()
  for i in range(5):
    log_query(
      conn,
      query_id=f"q{i}",
      text=f"q{i}",
      top_k=1,
      source_filter=None,
      since=None,
      until=None,
      results=[],
      ts=datetime(2026, 1, 1 + i, tzinfo=UTC),
    )
  rows = recent_queries(conn, limit=3)
  assert [r["id"] for r in rows] == ["q4", "q3", "q2"]
