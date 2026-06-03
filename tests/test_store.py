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


def test_store_items_replaces_entity_links_for_existing_item(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  item = Item(
    id=hash_id("email", "<replace@example.test>"),
    source="email",
    source_id="<replace@example.test>",
    timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    recipients=["bob@example.test"],
    content="Alice then Diana.",
  )

  store_items(conn, [item], [[0.1]], [[("Alice", "person", 1.0, "dictionary")]])
  store_items(conn, [item], [[0.1]], [[("Diana", "person", 1.0, "dictionary")]])

  rows = conn.execute(
    """
    SELECT e.canonical_name
    FROM item_entities ie
    JOIN entities e ON e.id = ie.entity_id
    WHERE ie.item_id = ?
    """,
    (item.id,),
  ).fetchall()
  assert [row["canonical_name"] for row in rows] == ["Diana"]


def test_store_items_updates_canonical_fields_on_reingest(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  base = Item(
    id=hash_id("tier2_ledger", "fact__example.md"),
    source="tier2_ledger",
    source_id="fact__example.md",
    timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
    sender="me",
    recipients=[],
    content="Original statement.",
    subject="Original title",
  )
  updated = Item(
    id=base.id,
    source=base.source,
    source_id=base.source_id,
    timestamp=datetime(2025, 2, 1, 9, 30, tzinfo=UTC),
    sender="me",
    recipients=[],
    content="Updated statement with new detail.",
    subject="Updated title",
  )

  store_items(conn, [base], [[0.1]], [[]])
  store_items(conn, [updated], [[0.2]], [[]])

  row = conn.execute(
    "SELECT content, subject, timestamp FROM items WHERE id = ?", (base.id,)
  ).fetchone()
  assert row["content"] == "Updated statement with new detail."
  assert row["subject"] == "Updated title"
  assert row["timestamp"].startswith("2025-02-01")

  fts_row = conn.execute(
    "SELECT content, subject FROM items_fts WHERE item_id = ?", (base.id,)
  ).fetchone()
  assert fts_row["content"] == "Updated statement with new detail."
  assert fts_row["subject"] == "Updated title"


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


def test_seed_entities_promotes_case_variant_pending_entity(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  item = Item(
    id=hash_id("email", "<case@example.test>"),
    source="email",
    source_id="<case@example.test>",
    timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    recipients=["bob@example.test"],
    content="alice appeared before dictionary promotion.",
  )
  store_items(conn, [item], [[0.1]], [[("alice", "person", 0.7, "ner")]])

  seed_entities(conn, [{"canonical": "Alice", "type": "person", "aliases": ["NC"]}])

  rows = conn.execute(
    "SELECT canonical_name, pending_review FROM entities"
  ).fetchall()
  assert [(row["canonical_name"], row["pending_review"]) for row in rows] == [
    ("Alice", 0)
  ]


def test_ner_entities_are_pending_review(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  item = Item(
    id=hash_id("email", "<ner@example.test>"),
    source="email",
    source_id="<ner@example.test>",
    timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    recipients=["bob@example.test"],
    content="A novel organization appeared.",
  )

  store_items(conn, [item], [[0.1]], [[("Novel Org", "org", 0.7, "ner")]])

  row = conn.execute(
    "SELECT pending_review FROM entities WHERE canonical_name = ?",
    ("Novel Org",),
  ).fetchone()
  assert row["pending_review"] == 1


def test_upsert_entity_folds_unicode_case(tmp_path):
  """SQLite's native lower() is ASCII-only; the override in open_db must
  make 'HØYRE' and 'Høyre' resolve to the same entity row."""
  from yaams.store import resolve_entity_id, upsert_entity

  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)

  with conn:
    first = upsert_entity(conn, "Høyre", "org", "ner")
    second = upsert_entity(conn, "HØYRE", "org", "ner")

  assert first == second
  assert resolve_entity_id(conn, "høyre") == first
  # canonical keeps the first-seen surface form for NER-sourced entities
  row = conn.execute(
    "SELECT canonical_name FROM entities WHERE id = ?", (first,)
  ).fetchone()
  assert row["canonical_name"] == "Høyre"


def test_vacuum_orphan_entities_spares_curated_denied_and_linked(tmp_path):
  from yaams.store import upsert_entity, vacuum_orphan_entities

  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)

  with conn:
    orphan = upsert_entity(conn, "Junk Fragment", "org", "ner")
    curated = upsert_entity(conn, "Norconsult", "org", "dictionary")
    linked = upsert_entity(conn, "Bærum", "place", "ner")
    denied = upsert_entity(conn, "Hei", "place", "ner")
    conn.execute("UPDATE entities SET pending_review = 2 WHERE id = ?", (denied,))
    conn.execute(
      """INSERT INTO items
           (id, source, source_id, timestamp, sender, recipients, content,
            ingested_at)
         VALUES ('item-1', 'test', 'sid-1', '2025-01-01T00:00:00+00:00',
                 'a@b.c', '[]', 'x', '2025-01-01T00:00:00+00:00')""",
    )
    conn.execute(
      "INSERT INTO item_entities (item_id, entity_id) VALUES ('item-1', ?)",
      (linked,),
    )

  dry = vacuum_orphan_entities(conn, dry_run=True)
  assert dry == {"deleted": 0, "orphans": 1}

  result = vacuum_orphan_entities(conn)
  assert result == {"deleted": 1, "orphans": 1}
  remaining = {
    r["id"] for r in conn.execute("SELECT id FROM entities").fetchall()
  }
  assert remaining == {curated, linked, denied}
  assert orphan not in remaining
