"""Tests for the scan-and-judge review queue (yaams.signals.review)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from yaams.retrieve import HybridResult, ScoreComponents
from yaams.schema import init_schema
from yaams.signals import (
  ReviewItem,
  ReviewResult,
  build_review_queue,
  dashboard_data,
  detect_provenance,
  flush_session,
  log_feedback,
  log_query,
  noise_cascade,
  render_dashboard,
  run_review_tui,
  score_query,
  verdict_signal,
)


def _open() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _insert_item(conn: sqlite3.Connection, rid: str, *, source: str = "imessage", content: str = "hello world", sender: str = "alice@example.test") -> None:
  conn.execute(
    """
    INSERT INTO items (id, source, source_id, timestamp, sender, recipients, content, subject, thread_id, ingested_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      rid,
      source,
      rid,
      "2026-04-01T12:00:00+00:00",
      sender,
      "[]",
      content,
      "",
      "t1",
      "2026-04-01T12:00:00+00:00",
    ),
  )
  conn.commit()


def _result(rid: str, *, source: str = "imessage", kind: str = "item") -> HybridResult:
  return HybridResult(
    id=rid,
    kind=kind,
    source=source,
    timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    subject="",
    content="hello",
    thread_id="t1",
    score=0.5,
    components=ScoreComponents(
      fts_rank=0, fts_score=-1.2, vector_rank=2, vector_distance=0.4, rrf_score=0.5
    ),
  )


def _log(
  conn: sqlite3.Connection,
  qid: str,
  *,
  text: str = "what happened",
  result_ids: list[str] | None = None,
  cited: list[str] = (),
  confidence: str | None = None,
  ts: datetime | None = None,
) -> None:
  ids = result_ids or []
  results = [_result(r) for r in ids]
  for r in ids:
    _insert_item(conn, r, content=f"body of {r}")
  log_query(
    conn,
    query_id=qid,
    text=text,
    top_k=10,
    source_filter=["imessage"],
    since=None,
    until=None,
    results=results,
    cited_result_ids=list(cited),
    confidence=confidence,
    ts=ts,
  )


# ---------------------------------------------------------------------------
# score_query
# ---------------------------------------------------------------------------


def test_score_unjudged_recent_low_confidence_ranks_highest():
  hi, hi_reasons = score_query(
    unjudged=True, results_returned=6, confidence="low",
    cited_count=0, age_days=0.1,
  )
  lo, _ = score_query(
    unjudged=False, results_returned=3, confidence="high",
    cited_count=1, age_days=30.0,
  )
  assert hi > lo
  assert "unjudged" in hi_reasons
  assert "low-confidence answer" in hi_reasons


def test_score_zero_results_gets_priority():
  prio, reasons = score_query(
    unjudged=True, results_returned=0, confidence=None,
    cited_count=0, age_days=5.0,
  )
  assert "zero results" in reasons
  assert prio > 1.0


# ---------------------------------------------------------------------------
# build_review_queue
# ---------------------------------------------------------------------------


def test_queue_includes_unjudged_excludes_judged_by_default():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])
  _log(conn, "q_b", result_ids=["r2"])
  log_feedback(conn, query_id="q_b", kind="hit", result_id="r2")

  queue = build_review_queue(conn)
  ids = [item.query_id for item in queue]
  assert ids == ["q_a"]


def test_queue_with_unjudged_only_false_returns_all():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])
  _log(conn, "q_b", result_ids=["r2"])
  log_feedback(conn, query_id="q_b", kind="hit", result_id="r2")

  queue = build_review_queue(conn, unjudged_only=False)
  ids = sorted(item.query_id for item in queue)
  assert ids == ["q_a", "q_b"]


def test_queue_priority_orders_low_confidence_above_high():
  conn = _open()
  now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  _log(
    conn,
    "q_high",
    result_ids=["r1", "r2"],
    cited=["r1"],
    confidence="high",
    ts=now,
  )
  _log(
    conn,
    "q_low",
    result_ids=["r3", "r4"],
    cited=["r3"],
    confidence="low",
    ts=now,
  )

  queue = build_review_queue(conn, now=now)
  assert queue[0].query_id == "q_low"
  assert queue[1].query_id == "q_high"


def test_queue_attaches_ranked_results_with_snippets():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1", "r2"], cited=["r1"])

  queue = build_review_queue(conn)
  assert len(queue) == 1
  item = queue[0]
  assert item.results_returned == 2
  assert len(item.results) == 2
  assert item.results[0].rank == 1
  assert item.results[0].result_id == "r1"
  assert item.results[0].cited is True
  assert "body of r1" in item.results[0].snippet
  assert item.results[1].cited is False


def test_queue_respects_limit():
  conn = _open()
  for i in range(5):
    _log(conn, f"q_{i}", result_ids=[f"r{i}"])

  queue = build_review_queue(conn, limit=2)
  assert len(queue) == 2


def test_queue_source_filter_substring_match():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])  # source_filter=["imessage"]
  # Manually insert a query with a different source filter
  conn.execute(
    """
    INSERT INTO queries (id, text, top_k, source_filter, results_returned, ts)
    VALUES (?, ?, ?, ?, ?, ?)
    """,
    ("q_b", "other", 10, '["email"]', 0, "2026-04-01T12:00:00+00:00"),
  )
  conn.commit()

  imessage_queue = build_review_queue(conn, source="imessage")
  assert [q.query_id for q in imessage_queue] == ["q_a"]
  email_queue = build_review_queue(conn, source="email")
  assert [q.query_id for q in email_queue] == ["q_b"]


# ---------------------------------------------------------------------------
# verdict_signal
# ---------------------------------------------------------------------------


def _item_with_results(ranks: list[int]) -> ReviewItem:
  results = [
    ReviewResult(
      rank=r,
      result_id=f"r{r}",
      kind="item",
      source="imessage",
      rrf_score=0.5,
      snippet="snippet",
      sender="alice",
      timestamp="2026-04-01T12:00:00+00:00",
      cited=False,
    )
    for r in ranks
  ]
  return ReviewItem(
    query_id="q_x",
    text="text",
    ts="2026-04-01T12:00:00+00:00",
    results_returned=len(ranks),
    shape=None,
    confidence=None,
    cited_count=0,
    results=results,
  )


def test_verdict_h_marks_hit_on_top_result():
  item = _item_with_results([1, 2, 3])
  out = verdict_signal(item, "h")
  assert out == {"query_id": "q_x", "kind": "hit", "result_id": "r1"}


def test_verdict_h_returns_none_when_no_results():
  item = _item_with_results([])
  assert verdict_signal(item, "h") is None


def test_verdict_m_marks_miss():
  item = _item_with_results([1])
  out = verdict_signal(item, "m")
  assert out == {"query_id": "q_x", "kind": "miss"}


def test_verdict_n_marks_noise():
  item = _item_with_results([1, 2])
  out = verdict_signal(item, "n")
  assert out == {"query_id": "q_x", "kind": "noise"}


def test_verdict_digit_marks_correction_at_rank():
  item = _item_with_results([1, 2, 3])
  out = verdict_signal(item, "3")
  assert out == {"query_id": "q_x", "kind": "correction", "result_id": "r3"}


def test_verdict_digit_out_of_range_returns_none():
  item = _item_with_results([1, 2])
  assert verdict_signal(item, "5") is None


@pytest.mark.parametrize("key", ["", " ", "q", "x", "0", "10"])
def test_verdict_unknown_keys_skip(key: str):
  item = _item_with_results([1, 2])
  assert verdict_signal(item, key) is None


# ---------------------------------------------------------------------------
# flush_session + dashboard
# ---------------------------------------------------------------------------


def test_flush_session_persists_entries_via_log_feedback():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1", "r2"])
  _log(conn, "q_b", result_ids=["r3"])

  queue = build_review_queue(conn)
  by_id = {item.query_id: item for item in queue}
  entries = [
    verdict_signal(by_id["q_a"], "h"),
    verdict_signal(by_id["q_b"], "m"),
  ]
  assert all(entries)

  written = flush_session(conn, entries)
  assert written == 2

  rows = conn.execute("SELECT query_id, kind, result_id FROM query_feedback ORDER BY query_id").fetchall()
  assert [(r["query_id"], r["kind"], r["result_id"]) for r in rows] == [
    ("q_a", "hit", "r1"),
    ("q_b", "miss", None),
  ]


def test_dashboard_data_counts_and_coverage():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])
  _log(conn, "q_b", result_ids=["r2"])
  _log(conn, "q_c", result_ids=["r3"])
  log_feedback(conn, query_id="q_a", kind="hit", result_id="r1")
  log_feedback(conn, query_id="q_b", kind="miss")

  data = dashboard_data(conn)
  assert data["total_queries"] == 3
  assert data["judged_queries"] == 2
  assert data["coverage"] == pytest.approx(2 / 3)
  assert data["by_kind"] == {"hit": 1, "miss": 1}
  assert data["hit_rate"] == pytest.approx(0.5)
  assert data["graded_queries"] == 2


def test_noise_cascade_marks_all_identical_text_unjudged():
  conn = _open()
  _log(conn, "q_a", text="anything", result_ids=["r1"])
  _log(conn, "q_b", text="anything", result_ids=["r2"])
  _log(conn, "q_c", text="anything", result_ids=["r3"])
  _log(conn, "q_d", text="different", result_ids=["r4"])
  # q_c already has a verdict — cascade should leave it alone.
  log_feedback(conn, query_id="q_c", kind="hit", result_id="r3")

  entries = noise_cascade(conn, query_id="q_a", text="anything")
  qids = sorted(e["query_id"] for e in entries)
  assert qids == ["q_a", "q_b"]
  assert all(e["kind"] == "noise" for e in entries)


def test_noise_cascade_includes_seed_even_if_judged():
  conn = _open()
  _log(conn, "q_a", text="anything", result_ids=["r1"])
  log_feedback(conn, query_id="q_a", kind="hit", result_id="r1")
  entries = noise_cascade(conn, query_id="q_a", text="anything")
  assert entries == [{"query_id": "q_a", "kind": "noise"}]


def test_dashboard_excludes_noise_from_hit_rate_but_counts_judged():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])
  _log(conn, "q_b", result_ids=["r2"])
  _log(conn, "q_c", result_ids=["r3"])
  log_feedback(conn, query_id="q_a", kind="hit", result_id="r1")
  log_feedback(conn, query_id="q_b", kind="noise")
  log_feedback(conn, query_id="q_c", kind="miss")

  data = dashboard_data(conn)
  assert data["judged_queries"] == 3
  assert data["coverage"] == pytest.approx(1.0)
  # Hit-rate denominator is graded only (hit + miss + correction), not noise.
  assert data["graded_queries"] == 2
  assert data["hit_rate"] == pytest.approx(0.5)
  assert data["noise_queries"] == 1


def test_render_dashboard_shows_noise_and_provenance():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])
  log_feedback(conn, query_id="q_a", kind="noise")
  text = render_dashboard(dashboard_data(conn))
  assert "Noise" in text
  # Tests log under "test" provenance via PYTEST_CURRENT_TEST.
  assert "test" in text
  assert "By provenance" in text


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_detect_provenance_explicit_wins():
  assert detect_provenance("cli") == "cli"
  assert detect_provenance("hugr") == "hugr"


def test_detect_provenance_falls_through_to_pytest_when_none(monkeypatch):
  # We *are* under pytest, so PYTEST_CURRENT_TEST is set.
  assert detect_provenance(None) == "test"


def test_detect_provenance_unknown_outside_pytest(monkeypatch):
  monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
  assert detect_provenance(None) == "unknown"


def test_log_query_persists_provenance():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])  # uses log_query under the hood
  row = conn.execute("SELECT provenance FROM queries WHERE id = 'q_a'").fetchone()
  # PYTEST_CURRENT_TEST is set during this test, so log_query auto-tags it.
  assert row["provenance"] == "test"


def test_log_query_explicit_provenance_overrides_pytest():
  conn = _open()
  log_query(
    conn,
    query_id="q_explicit",
    text="hello",
    top_k=10,
    source_filter=None,
    since=None,
    until=None,
    results=[],
    provenance="cli",
  )
  row = conn.execute("SELECT provenance FROM queries WHERE id = 'q_explicit'").fetchone()
  assert row["provenance"] == "cli"


def test_run_review_tui_empty_queue_short_circuits(capsys):
  conn = _open()
  summary = run_review_tui(conn, [])
  assert summary == {"judged": 0, "entries": []}
  out = capsys.readouterr().out
  assert "empty" in out.lower()


def test_render_dashboard_smoke():
  conn = _open()
  _log(conn, "q_a", result_ids=["r1"])
  log_feedback(conn, query_id="q_a", kind="hit", result_id="r1")

  text = render_dashboard(dashboard_data(conn))
  assert "Coverage" in text
  assert "Hit rate" in text
