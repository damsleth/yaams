"""Tests for `yaams ingest --full`: watermark bypass + monotonic watermark.

`--full` makes one run behave like the first run ever did - each source
re-walks history from the configured `ingest.since` instead of its stored
watermark. The invariant that makes this safe is in `ingest_source`: the
watermark only ever moves forward, so a full re-walk that scans nothing
newer (or nothing at all) cannot rewind it.
"""
from __future__ import annotations

from datetime import UTC, datetime

from yaams.cli.ingest import _effective_since, ingest_source
from yaams.db import open_db
from yaams.schema import init_schema
from yaams.watermark import get_watermark, update_watermark

_CONFIGURED = "2025-01-01T00:00:00Z"
_CFG = {"ingest": {"since": _CONFIGURED}}


def _conn(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  return conn


class _StubAdapter:
  """No scanned_through, no skip counters - the minimal adapter surface."""


def test_effective_since_defaults_to_watermark(tmp_path):
  conn = _conn(tmp_path)
  mark = datetime(2026, 6, 1, tzinfo=UTC)
  with conn:
    update_watermark(conn, "email", mark)
  assert _effective_since(conn, "email", _CFG) == mark


def test_effective_since_full_ignores_watermark(tmp_path):
  conn = _conn(tmp_path)
  with conn:
    update_watermark(conn, "email", datetime(2026, 6, 1, tzinfo=UTC))
  assert _effective_since(conn, "email", _CFG, full=True) == datetime(
    2025, 1, 1, tzinfo=UTC
  )


def test_effective_since_full_matches_first_run(tmp_path):
  conn = _conn(tmp_path)  # no watermark stored: an actual first run
  assert _effective_since(conn, "email", _CFG, full=True) == _effective_since(
    conn, "email", _CFG
  )


def test_full_rescan_never_rewinds_watermark(tmp_path):
  conn = _conn(tmp_path)
  mark = datetime(2026, 6, 1, tzinfo=UTC)
  with conn:
    update_watermark(conn, "email", mark)

  # A --full re-walk: since is far behind the watermark and the adapter
  # yields nothing (everything filtered / source empty). latest_ts would be
  # `since` without the monotonic guard.
  old_since = datetime(2025, 1, 1, tzinfo=UTC)
  stats = ingest_source(
    conn,
    "email",
    _StubAdapter(),
    [],
    old_since,
    batch_size=8,
    dry_run=False,
    processors=None,
    started_at=datetime.now(UTC),
    fetch_ms=0.0,
  )
  assert stats["seen"] == 0
  assert get_watermark(conn, "email") == mark
