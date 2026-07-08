"""Tests for the scan-and-judge review queue (yaams.signals.review)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from yaams.retrieve import HybridResult, ScoreComponents
from yaams.schema import init_schema
from yaams.signals import (
  ReviewItem,
  ReviewResult,
  build_review_queue,
  dashboard_data,
  default_verdict,
  detect_provenance,
  flush_session,
  log_feedback,
  log_query,
  noise_cascade,
  render_card_lines,
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
  shape: str | None = None,
  parser_fallback: bool = False,
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
    shape=shape,
    parser_fallback=parser_fallback,
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


def test_queue_provenance_filter():
  conn = _open()
  log_query(conn, query_id="q_cli", text="cli query", top_k=5, source_filter=None,
            since=None, until=None, results=[], provenance="cli")
  log_query(conn, query_id="q_mcp", text="agent query", top_k=5, source_filter=None,
            since=None, until=None, results=[], provenance="mcp")
  ids = [item.query_id for item in build_review_queue(conn, provenance="mcp")]
  assert ids == ["q_mcp"]


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


def _item_with_results(
  ranks: list[int], shape: str | None = None, parser_fallback: bool = False
) -> ReviewItem:
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
    shape=shape,
    confidence=None,
    cited_count=0,
    results=results,
    parser_fallback=parser_fallback,
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
# shape gating
# ---------------------------------------------------------------------------


def test_is_answer_shaped_classification():
  from yaams.signals import is_answer_shaped

  for s in ("factual", "first_occurrence", "last_occurrence", "event_anchored"):
    assert is_answer_shaped(s) is True
  for s in ("synthesis", "temporal_range", "SYNTHESIS", " synthesis "):
    assert is_answer_shaped(s) is False
  # Unknown / None default to answer-shaped (backward compatible).
  assert is_answer_shaped(None) is True
  assert is_answer_shaped("whatever") is True


def test_is_answer_shaped_fallback_overrides_shape():
  from yaams.signals import is_answer_shaped

  # A fallback query's stored shape is a placeholder — even "factual" must
  # grade usefulness, since the parser never understood the query.
  assert is_answer_shaped("factual", parser_fallback=True) is False
  assert is_answer_shaped(None, parser_fallback=True) is False
  # Confidently parsed factual stays answer-shaped.
  assert is_answer_shaped("factual", parser_fallback=False) is True


def test_fallback_query_grades_set_usefulness():
  # The original bug: a bag-of-keywords query logged as fallback→factual was
  # graded on the answer rubric. It should grade usefulness instead.
  item = _item_with_results([1, 2, 3], shape="factual", parser_fallback=True)
  assert verdict_signal(item, "r") == {"query_id": "q_x", "kind": "relevant"}
  assert verdict_signal(item, "t") == {"query_id": "q_x", "kind": "thin"}
  assert verdict_signal(item, "h") is None
  assert verdict_signal(item, "2") is None


def test_queue_surfaces_parser_fallback():
  conn = _open()
  _log(conn, "q_fb", result_ids=["r1"], shape="factual", parser_fallback=True)
  _log(conn, "q_ok", result_ids=["r2"], shape="factual", parser_fallback=False)
  by_id = {it.query_id: it for it in build_review_queue(conn)}
  assert by_id["q_fb"].parser_fallback is True
  assert by_id["q_ok"].parser_fallback is False
  # And the gating follows: fallback → recall verdicts, confident → answer.
  assert verdict_signal(by_id["q_fb"], "r") == {"query_id": "q_fb", "kind": "relevant"}
  assert verdict_signal(by_id["q_ok"], "h") == {
    "query_id": "q_ok", "kind": "hit", "result_id": "r2",
  }


@pytest.mark.parametrize("shape", ["synthesis", "temporal_range"])
def test_recall_shape_grades_set_usefulness(shape):
  item = _item_with_results([1, 2, 3], shape=shape)
  assert verdict_signal(item, "r") == {"query_id": "q_x", "kind": "relevant"}
  assert verdict_signal(item, "t") == {"query_id": "q_x", "kind": "thin"}
  # Answer-precision keys don't apply to a recall query.
  assert verdict_signal(item, "h") is None
  assert verdict_signal(item, "m") is None
  assert verdict_signal(item, "2") is None
  # Noise is shared across shapes.
  assert verdict_signal(item, "n") == {"query_id": "q_x", "kind": "noise"}


@pytest.mark.parametrize("shape", ["factual", "last_occurrence", None])
def test_answer_shape_rejects_recall_verdicts(shape):
  item = _item_with_results([1, 2, 3], shape=shape)
  # relevant/thin only make sense for recall-shaped queries.
  assert verdict_signal(item, "r") is None
  assert verdict_signal(item, "t") is None
  # Answer keys still work.
  assert verdict_signal(item, "h") == {
    "query_id": "q_x", "kind": "hit", "result_id": "r1",
  }


def test_dashboard_usefulness_rate_separate_from_hit_rate():
  conn = _open()
  _log(conn, "q_ans")
  _log(conn, "q_rec1")
  _log(conn, "q_rec2")
  flush_session(conn, [
    {"query_id": "q_ans", "kind": "hit", "result_id": "r1"},
    {"query_id": "q_rec1", "kind": "relevant"},
    {"query_id": "q_rec2", "kind": "thin"},
  ])
  data = dashboard_data(conn)
  # Answer axis: 1 hit of 1 graded → 100%, and recall verdicts excluded.
  assert data["graded_queries"] == 1
  assert data["hit_rate"] == 1.0
  # Recall axis: 1 relevant of 2 graded → 50%.
  assert data["graded_recall_queries"] == 2
  assert data["usefulness_rate"] == 0.5
  text = render_dashboard(data)
  assert "Usefulness" in text


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
  assert detect_provenance("import") == "import"


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


# ---------------------------------------------------------------------------
# default_verdict (step 2)
# ---------------------------------------------------------------------------


def _item_with_snippet(
  text: str,
  snippets: list[str],
  shape: str | None = "factual",
  parser_fallback: bool = False,
) -> ReviewItem:
  results = [
    ReviewResult(
      rank=i + 1,
      result_id=f"r{i+1}",
      kind="item",
      source="imessage",
      rrf_score=0.5,
      snippet=snippets[i],
      sender="alice",
      timestamp="2026-04-01T12:00:00+00:00",
      cited=False,
    )
    for i in range(len(snippets))
  ]
  return ReviewItem(
    query_id="q_dv",
    text=text,
    ts="2026-04-01T12:00:00+00:00",
    results_returned=len(snippets),
    shape=shape,
    confidence=None,
    cited_count=0,
    results=results,
    parser_fallback=parser_fallback,
  )


def test_default_verdict_noise_for_probe_query():
  # Very short query → noise.
  item = _item_with_snippet("hi", ["anything relevant"], shape="factual")
  assert default_verdict(item) == "noise"


def test_default_verdict_noise_for_test_query():
  item = _item_with_snippet("test something", ["some content about testing"], shape="factual")
  assert default_verdict(item) == "noise"


def test_default_verdict_noise_for_question_mark_prefix():
  item = _item_with_snippet("?what is foo", ["foo bar baz"], shape="factual")
  assert default_verdict(item) == "noise"


def test_default_verdict_miss_when_no_tokens_in_any_snippet():
  item = _item_with_snippet(
    "crayon project budget",
    ["unrelated text xyz", "another unrelated snippet"],
    shape="factual",
  )
  assert default_verdict(item) == "miss"


def test_default_verdict_hit_when_token_in_rank1_answer_shaped():
  item = _item_with_snippet(
    "crayon project budget",
    ["crayon allocated budget for Q3", "something else"],
    shape="factual",
  )
  assert default_verdict(item) == "hit"


def test_default_verdict_relevant_when_token_in_rank1_recall_shaped():
  item = _item_with_snippet(
    "crayon project updates",
    ["crayon sent project updates last week", "something else"],
    shape="synthesis",
  )
  assert default_verdict(item) == "relevant"


def test_default_verdict_none_when_token_only_in_lower_rank():
  # Token in rank 2 but not rank 1 → no confident default.
  item = _item_with_snippet(
    "crayon project budget",
    ["unrelated text here", "crayon project budget details"],
    shape="factual",
  )
  assert default_verdict(item) is None


def test_default_verdict_fallback_query_relevant_not_hit():
  # parser_fallback → recall-shaped even if shape="factual".
  item = _item_with_snippet(
    "crayon project budget",
    ["crayon project budget details"],
    shape="factual",
    parser_fallback=True,
  )
  assert default_verdict(item) == "relevant"


# ---------------------------------------------------------------------------
# render_card_lines (step 1 — pure renderer)
# ---------------------------------------------------------------------------


def test_render_card_lines_rank1_expanded_others_collapsed():
  item = _item_with_snippet(
    "what happened at crayon",
    ["crayon had a big meeting", "another result snippet", "third result"],
    shape="factual",
  )
  lines = render_card_lines(item, expanded_ranks={1}, width=80)
  joined = "\n".join(lines)

  # Rank 1 header and snippet should appear.
  assert "crayon had a big meeting" in joined
  # Ranks 2 and 3 should be collapsed to one-line summary.
  assert "[2]" in joined
  assert "[3]" in joined
  # The collapsed lines should NOT contain the snippets for ranks 2/3.
  assert "another result snippet" not in joined
  assert "third result" not in joined


def test_render_card_lines_all_expanded():
  item = _item_with_snippet(
    "what happened at crayon",
    ["crayon had a big meeting", "another result snippet"],
    shape="factual",
  )
  lines = render_card_lines(item, expanded_ranks={1, 2}, width=80)
  joined = "\n".join(lines)
  assert "crayon had a big meeting" in joined
  assert "another result snippet" in joined


def test_render_card_lines_default_is_rank1_only():
  item = _item_with_snippet(
    "what happened at crayon",
    ["crayon had a big meeting", "another result snippet"],
    shape="factual",
  )
  # Default: no expanded_ranks arg → only rank 1 shown.
  lines = render_card_lines(item, width=80)
  joined = "\n".join(lines)
  assert "crayon had a big meeting" in joined
  assert "another result snippet" not in joined


def test_render_card_lines_shows_default_verdict_in_keybar():
  item = _item_with_snippet(
    "what happened at crayon",
    ["crayon had a big meeting"],
    shape="factual",
  )
  lines = render_card_lines(item, width=80)
  keybar = lines[-1]
  # default_verdict should be "hit" (token "crayon" in rank-1 snippet).
  assert "enter=hit(default)" in keybar


def test_render_card_lines_no_default_when_token_only_in_lower_rank():
  # Token "crayon" appears only in rank 2, not rank 1 → no default verdict.
  item = _item_with_snippet(
    "crayon project budget",
    ["completely unrelated content here", "crayon project budget details"],
    shape="factual",
  )
  lines = render_card_lines(item, expanded_ranks={1}, width=80)
  keybar = lines[-1]
  assert "enter=" not in keybar


def test_render_card_lines_rank1_snippet_truncated_to_480():
  long_snippet = "word " * 200  # much longer than 480 chars
  item = _item_with_snippet(
    "word query here",
    [long_snippet],
    shape="factual",
  )
  lines = render_card_lines(item, expanded_ranks={1}, width=80)
  joined = "\n".join(lines)
  # The joined snippet section should be truncated.
  assert "…" in joined
