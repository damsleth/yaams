from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from yaams.cli import _format_duration, _record_ingest_run
from yaams.schema import init_schema


@pytest.fixture()
def conn():
  c = sqlite3.connect(":memory:")
  init_schema(c, embedding_dim=1024, use_vec=False)
  yield c
  c.close()


def test_ingest_runs_table_exists(conn):
  row = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='ingest_runs'"
  ).fetchone()
  assert row is not None


def test_record_ingest_run_success(conn):
  started = datetime(2026, 5, 10, 2, 0, 0, tzinfo=UTC)
  ended = datetime(2026, 5, 10, 2, 0, 5, tzinfo=UTC)
  _record_ingest_run(
    conn,
    run_id="abc123",
    source="imessage",
    started_at=started,
    ended_at=ended,
    duration_ms=5000.0,
    seen=42,
    new=10,
    skipped=2,
    status="success",
    error=None,
  )
  conn.commit()
  row = conn.execute(
    "SELECT run_id, source, items_seen, items_new, items_skipped, status, error"
    " FROM ingest_runs WHERE run_id = ?",
    ("abc123",),
  ).fetchone()
  assert row == ("abc123", "imessage", 42, 10, 2, "success", None)


def test_record_ingest_run_failure(conn):
  now = datetime.now(UTC)
  _record_ingest_run(
    conn,
    run_id="failed_run",
    source="signal",
    started_at=now,
    ended_at=now,
    duration_ms=12.5,
    seen=0,
    new=0,
    skipped=0,
    status="failed",
    error="OSError: chat.db not found",
  )
  conn.commit()
  status, error = conn.execute(
    "SELECT status, error FROM ingest_runs WHERE run_id = 'failed_run'"
  ).fetchone()
  assert status == "failed"
  assert "chat.db" in error


def test_record_ingest_run_indices_present(conn):
  indices = {
    row[0]
    for row in conn.execute(
      "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ingest_runs'"
    )
  }
  assert "idx_ingest_runs_run_id" in indices
  assert "idx_ingest_runs_source_time" in indices


def test_format_duration_milliseconds():
  assert _format_duration(0) == "0ms"
  assert _format_duration(500) == "500ms"
  assert _format_duration(999) == "999ms"


def test_format_duration_seconds():
  assert _format_duration(1000) == "1.0s"
  assert _format_duration(12_345) == "12.3s"
  assert _format_duration(59_999) == "60.0s"


def test_format_duration_minutes():
  assert _format_duration(60_000) == "1m00.0s"
  assert _format_duration(125_000) == "2m05.0s"
  assert _format_duration(3_660_000) == "61m00.0s"
