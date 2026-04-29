from __future__ import annotations

from datetime import UTC, datetime

from yaams.db import open_db
from yaams.ingest.base import Item, hash_id
from yaams.schema import init_schema
from yaams.store import seed_entities, store_items


def test_store_items_is_idempotent(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  item = Item(
    id=hash_id("email", "<a@example.test>"),
    source="email",
    source_id="<a@example.test>",
    timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    recipients=["bob@example.test"],
    content="Alice and Em discussed YAAMS.",
    subject="Memory",
  )

  tags = [[("Alice", "person", 1.0, "dictionary")]]
  embeddings = [[0.1, 0.2, 0.3]]

  first = store_items(conn, [item], embeddings, tags)
  second = store_items(conn, [item], embeddings, tags)

  assert first.items_inserted == 1
  assert second.items_inserted == 0
  assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
  assert conn.execute("SELECT count(*) FROM items_vec").fetchone()[0] == 1
  assert conn.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 1
  assert conn.execute("SELECT count(*) FROM item_entities").fetchone()[0] == 1


def test_open_db_readonly_does_not_require_writes(tmp_path):
  path = tmp_path / "yaams.db"
  conn = open_db(path)
  init_schema(conn, use_vec=False)
  conn.close()

  readonly = open_db(path, readonly=True)
  try:
    assert readonly.execute("SELECT count(*) FROM items").fetchone()[0] == 0
  finally:
    readonly.close()


def test_seed_entities_promotes_dictionary_entries(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  seed_entities(
    conn,
    [
      {
        "canonical": "Local Aid Society",
        "type": "org",
        "aliases": ["LAS", "Local Aid"],
      }
    ],
  )

  row = conn.execute(
    "SELECT entity_type, aliases, pending_review FROM entities WHERE canonical_name = ?",
    ("Local Aid Society",),
  ).fetchone()

  assert row["entity_type"] == "org"
  assert "LAS" in row["aliases"]
  assert row["pending_review"] == 0
