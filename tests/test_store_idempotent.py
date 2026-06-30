"""Guards for the raw-store invariant (AGENTS.md "Raw-store invariants").

Idempotent re-ingest is already pinned by
``test_store.test_store_items_is_idempotent``. These cover the two parts the
invariant adds on top: deterministic ids, and that a mutable source's revision
(encoded in source_id) lands as a NEW row rather than rewriting the original.
"""
from __future__ import annotations

from datetime import UTC, datetime

from yaams.db import open_db
from yaams.ingest.base import Item, hash_id
from yaams.schema import init_schema
from yaams.store import store_items


def test_hash_id_is_deterministic_and_distinct():
  assert hash_id("email", "<a@x>") == hash_id("email", "<a@x>")
  assert hash_id("email", "<a@x>") != hash_id("email", "<b@x>")
  # source is part of the key, so the same source_id under a different source
  # never collides.
  assert hash_id("email", "<a@x>") != hash_id("teams", "<a@x>")


def _item(source: str, source_id: str, content: str) -> Item:
  return Item(
    id=hash_id(source, source_id),
    source=source,
    source_id=source_id,
    timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
    sender="a@x",
    recipients=["b@x"],
    content=content,
  )


def test_revision_in_source_id_appends_a_new_row(tmp_path):
  # A mutable upstream item encodes its revision in source_id, so an edit is a
  # new (source, source_id) -> new id -> a new row. History is preserved and the
  # original is never rewritten.
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  v1 = _item("mail_work", "msg42:rev1", "first draft")
  v2 = _item("mail_work", "msg42:rev2", "edited draft")

  store_items(conn, [v1], [[0.1]], [[]])
  store_items(conn, [v2], [[0.1]], [[]])

  assert v1.id != v2.id
  rows = conn.execute(
    "SELECT content FROM items WHERE source_id LIKE 'msg42:%' ORDER BY source_id"
  ).fetchall()
  assert [row[0] for row in rows] == ["first draft", "edited draft"]
