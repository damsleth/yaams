"""Migration 0006: items.provenance column + ingest population + legacy fallback."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from yaams.ingest.base import Item, hash_id
from yaams.migrations import apply_pending
from yaams.retrieve import HybridResult, attach_trust_verdicts
from yaams.schema import init_schema
from yaams.store import store_items


def _cols(conn, table) -> set[str]:
  return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_has_provenance_column():
  conn = sqlite3.connect(":memory:")
  init_schema(conn, embedding_dim=4, use_vec=False)
  assert "provenance" in _cols(conn, "items")


def test_migration_is_applied_in_journal():
  conn = sqlite3.connect(":memory:")
  init_schema(conn, embedding_dim=4, use_vec=False)
  names = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
  assert "0006_items_provenance" in names


def test_ingest_populates_provenance():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  item = Item(
    id=hash_id("email", "x"),
    source="email",
    source_id="x",
    timestamp=datetime(2026, 5, 1, tzinfo=UTC),
    sender="a@test",
    recipients=[],
    content="body",
    subject="",
  )
  store_items(conn, [item], [b"\x00" * 16], [[]])
  row = conn.execute("SELECT provenance FROM items WHERE id = ?", (item.id,)).fetchone()
  assert row["provenance"] == "authored"


def test_migration_idempotent_on_rerun():
  conn = sqlite3.connect(":memory:")
  init_schema(conn, embedding_dim=4, use_vec=False)
  # Re-running apply_pending must not error or duplicate the column.
  apply_pending(conn)
  cols = [c for c in _cols(conn, "items")]
  assert cols.count("provenance") == 1


def test_legacy_null_provenance_derives_at_query_time():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  item = Item(
    id=hash_id("github", "y"),
    source="github",
    source_id="y",
    timestamp=datetime(2026, 5, 1, tzinfo=UTC),
    sender="a@test",
    recipients=[],
    content="body",
    subject="",
  )
  store_items(conn, [item], [b"\x00" * 16], [[]])
  # Simulate a row written before the column existed.
  conn.execute("UPDATE items SET provenance = NULL WHERE id = ?", (item.id,))
  results = [
    HybridResult(
      id=item.id,
      kind="item",
      source="github",
      timestamp=datetime(2026, 5, 1, tzinfo=UTC),
      sender="a@test",
      subject="",
      content="",
      thread_id=None,
      score=0.9,
    )
  ]
  # github -> structured (0.90) clears the high band when weighting is on,
  # proving provenance was derived from source despite the NULL column.
  attach_trust_verdicts(results, conn, provenance_weighting_enabled=True)
  assert results[0].trust.level == "high"
