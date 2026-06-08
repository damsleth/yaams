"""`--reindex` re-stores already-known items so derived fields refresh.

Default ingest skips items whose id already exists (insert-only, dedup by id).
That assumes a known id carries identical content — false when an ingester
changes how a *derived* field like the timestamp is computed. `--reindex`
keeps known items so the store UPDATE path runs.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

from yaams.cli.ingest import process_batch
from yaams.ingest.base import Item, hash_id
from yaams.schema import init_schema


def _conn():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _processors():
  return SimpleNamespace(
    embedder=SimpleNamespace(embed_batch=lambda texts: [[0.0] * 4 for _ in texts]),
    tagger=SimpleNamespace(tag=lambda text: []),
  )


def _item(ts: datetime, inferred: bool = False) -> Item:
  return Item(
    id=hash_id("notes", "a.md"),
    source="notes",
    source_id="a.md",
    timestamp=ts,
    sender="me",
    recipients=[],
    content="same content always",
    timestamp_inferred=inferred,
  )


def _stored_ts(conn) -> tuple[str, int]:
  row = conn.execute(
    "SELECT timestamp, timestamp_inferred FROM items WHERE source='notes'"
  ).fetchone()
  return row["timestamp"], row["timestamp_inferred"]


def test_default_ingest_skips_known_id():
  conn = _conn()
  procs = _processors()
  old = datetime(2026, 5, 22, tzinfo=UTC)
  assert process_batch(conn, [_item(old, inferred=True)], procs, dry_run=False) == 1

  # Same id, corrected timestamp — default run drops it as known: no update.
  new = datetime(2026, 1, 3, tzinfo=UTC)
  assert process_batch(conn, [_item(new, inferred=False)], procs, dry_run=False) == 0
  ts, inferred = _stored_ts(conn)
  assert ts.startswith("2026-05-22")
  assert inferred == 1


def test_reindex_refreshes_derived_fields():
  conn = _conn()
  procs = _processors()
  old = datetime(2026, 5, 22, tzinfo=UTC)
  process_batch(conn, [_item(old, inferred=True)], procs, dry_run=False)

  new = datetime(2026, 1, 3, tzinfo=UTC)
  # reindex keeps the known item; store UPDATE path runs (0 inserted).
  assert process_batch(conn, [_item(new, inferred=False)], procs, dry_run=False, reindex=True) == 0
  ts, inferred = _stored_ts(conn)
  assert ts.startswith("2026-01-03")
  assert inferred == 0
