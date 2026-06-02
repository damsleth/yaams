"""Tests for the inline post-query feedback prompt (yaams query --prompt)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import click

from yaams.cli.query import _prompt_feedback, _should_prompt
from yaams.retrieve import HybridResult, ScoreComponents
from yaams.schema import init_schema
from yaams.signals import log_query


def _open_at(path: Path) -> sqlite3.Connection:
  conn = sqlite3.connect(str(path))
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _result(rid: str) -> HybridResult:
  from datetime import UTC, datetime

  return HybridResult(
    id=rid,
    kind="item",
    source="imessage",
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


# ---------------------------------------------------------------------------
# _should_prompt
# ---------------------------------------------------------------------------


def test_should_prompt_explicit_true_wins():
  assert _should_prompt(True, "text") is True


def test_should_prompt_explicit_false_wins():
  assert _should_prompt(False, "text") is False


def test_should_prompt_never_in_json_mode():
  assert _should_prompt(None, "json") is False
  # Even an explicit --prompt is overridden by JSON output (no place to render).
  assert _should_prompt(True, "json") is False


def test_should_prompt_default_off_when_not_tty(monkeypatch):
  monkeypatch.setattr("sys.stdin.isatty", lambda: False)
  monkeypatch.setattr("sys.stdout.isatty", lambda: True)
  assert _should_prompt(None, "text") is False


def test_should_prompt_default_on_when_both_ttys(monkeypatch):
  monkeypatch.setattr("sys.stdin.isatty", lambda: True)
  monkeypatch.setattr("sys.stdout.isatty", lambda: True)
  assert _should_prompt(None, "text") is True


# ---------------------------------------------------------------------------
# _prompt_feedback — exercise each verdict branch with click.getchar patched
# ---------------------------------------------------------------------------


def _seed_query(db_path: Path, qid: str = "q_x", text: str = "hello") -> list[HybridResult]:
  conn = _open_at(db_path)
  try:
    results = [_result("r1"), _result("r2"), _result("r3")]
    log_query(
      conn,
      query_id=qid,
      text=text,
      top_k=10,
      source_filter=None,
      since=None,
      until=None,
      results=results,
    )
  finally:
    conn.close()
  return results


def _fetch_feedback(db_path: Path) -> list[tuple]:
  conn = sqlite3.connect(str(db_path))
  try:
    return conn.execute(
      "SELECT query_id, kind, result_id FROM query_feedback ORDER BY id"
    ).fetchall()
  finally:
    conn.close()


def test_prompt_h_logs_hit_on_top(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  results = _seed_query(db)
  monkeypatch.setattr(click, "getchar", lambda echo=True: "h")
  _prompt_feedback(db_path=db, query_id="q_x", query_text="hello", results=results)
  rows = _fetch_feedback(db)
  assert rows == [("q_x", "hit", "r1")]


def test_prompt_m_logs_miss(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  results = _seed_query(db)
  monkeypatch.setattr(click, "getchar", lambda echo=True: "m")
  _prompt_feedback(db_path=db, query_id="q_x", query_text="hello", results=results)
  rows = _fetch_feedback(db)
  assert rows == [("q_x", "miss", None)]


def test_prompt_digit_logs_correction_at_rank(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  results = _seed_query(db)
  monkeypatch.setattr(click, "getchar", lambda echo=True: "2")
  _prompt_feedback(db_path=db, query_id="q_x", query_text="hello", results=results)
  rows = _fetch_feedback(db)
  assert rows == [("q_x", "correction", "r2")]


def test_prompt_digit_out_of_range_logs_nothing(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  results = _seed_query(db)
  monkeypatch.setattr(click, "getchar", lambda echo=True: "9")
  _prompt_feedback(db_path=db, query_id="q_x", query_text="hello", results=results)
  assert _fetch_feedback(db) == []


def test_prompt_n_cascades_noise(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  # Seed three queries with identical text — n should cascade across all.
  conn = _open_at(db)
  try:
    rs = [_result("r1"), _result("r2")]
    log_query(conn, query_id="q_a", text="anything", top_k=10,
              source_filter=None, since=None, until=None, results=rs)
    log_query(conn, query_id="q_b", text="anything", top_k=10,
              source_filter=None, since=None, until=None, results=rs)
    log_query(conn, query_id="q_c", text="anything", top_k=10,
              source_filter=None, since=None, until=None, results=rs)
  finally:
    conn.close()

  monkeypatch.setattr(click, "getchar", lambda echo=True: "n")
  _prompt_feedback(db_path=db, query_id="q_a", query_text="anything", results=rs)
  rows = _fetch_feedback(db)
  noise_ids = sorted(r[0] for r in rows if r[1] == "noise")
  assert noise_ids == ["q_a", "q_b", "q_c"]


def test_prompt_enter_skips(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  results = _seed_query(db)
  monkeypatch.setattr(click, "getchar", lambda echo=True: "\r")
  _prompt_feedback(db_path=db, query_id="q_x", query_text="hello", results=results)
  assert _fetch_feedback(db) == []


def test_prompt_keyboard_interrupt_skips_cleanly(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  results = _seed_query(db)

  def _raise(echo=True):
    raise KeyboardInterrupt

  monkeypatch.setattr(click, "getchar", _raise)
  _prompt_feedback(db_path=db, query_id="q_x", query_text="hello", results=results)
  assert _fetch_feedback(db) == []


def test_prompt_no_results_returns_immediately(tmp_path, monkeypatch):
  db = tmp_path / "data.db"
  _seed_query(db)
  called = {"hit": False}

  def _track(echo=True):
    called["hit"] = True
    return "h"

  monkeypatch.setattr(click, "getchar", _track)
  _prompt_feedback(db_path=db, query_id="q_x", query_text="hello", results=[])
  assert called["hit"] is False
