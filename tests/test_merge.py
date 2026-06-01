from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from yaams.ingest.base import Item, hash_id
from yaams.retrieve.associate import build_cooccurrence, resolve_associations
from yaams.schema import init_schema
from yaams.store import (
  add_entity_tags,
  get_entity_meta,
  get_entity_tags,
  merge_entities,
  prune_entity,
  resolve_entity_id,
  set_entity_meta,
  store_items,
)


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _ent(conn, name, etype="org", pending=1):
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
    "VALUES (?, ?, '[]', ?)",
    (name, etype, pending),
  )
  conn.commit()
  return resolve_entity_id(conn, name)


def _item(conn, msg_id, entity_ids, content="hello"):
  it = Item(
    id=hash_id("imessage", f"t:{msg_id}"),
    source="imessage", source_id=f"t:{msg_id}",
    timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    sender="a@test", recipients=[], content=content, subject="", thread_id="t",
  )
  store_items(conn, [it], [b"\x00" * 16], [[]])
  for eid in entity_ids:
    conn.execute(
      "INSERT INTO item_entities (item_id, entity_id, confidence, source) VALUES (?, ?, ?, 'ner')",
      (it.id, eid, 0.7),
    )
  conn.commit()
  return it.id


def test_merge_repoints_item_links_and_deletes_victim():
  conn = _open_db()
  survivor = _ent(conn, "Crayon")
  victim = _ent(conn, "Crayon AS")
  i1 = _item(conn, "1", [victim])
  _item(conn, "2", [survivor])

  stats = merge_entities(conn, survivor, [victim])
  assert stats["victims"] == 1
  assert resolve_entity_id(conn, "Crayon AS") is None  # victim gone
  # i1's link now points at the survivor
  rows = conn.execute(
    "SELECT entity_id FROM item_entities WHERE item_id = ?", (i1,)
  ).fetchall()
  assert [r["entity_id"] for r in rows] == [survivor]


def test_merge_dedupes_shared_item_keeping_max_confidence():
  conn = _open_db()
  survivor = _ent(conn, "Crayon")
  victim = _ent(conn, "Crayon Group")
  it = Item(
    id=hash_id("imessage", "t:1"), source="imessage", source_id="t:1",
    timestamp=datetime(2026, 4, 1, tzinfo=UTC), sender="a", recipients=[],
    content="x", subject="", thread_id="t",
  )
  store_items(conn, [it], [b"\x00" * 16], [[]])
  conn.execute("INSERT INTO item_entities (item_id, entity_id, confidence, source) VALUES (?,?,?,'dictionary')", (it.id, survivor, 1.0))
  conn.execute("INSERT INTO item_entities (item_id, entity_id, confidence, source) VALUES (?,?,?,'ner')", (it.id, victim, 0.7))
  conn.commit()

  merge_entities(conn, survivor, [victim])
  rows = conn.execute(
    "SELECT entity_id, confidence FROM item_entities WHERE item_id = ?", (it.id,)
  ).fetchall()
  assert len(rows) == 1
  assert rows[0]["entity_id"] == survivor
  assert rows[0]["confidence"] == 1.0  # max kept


def test_merge_folds_tags_meta_and_relations():
  conn = _open_db()
  survivor = _ent(conn, "Crayon")
  victim = _ent(conn, "Crayon AS")
  other = _ent(conn, "Norconsult")
  add_entity_tags(conn, survivor, ["customer"])
  add_entity_tags(conn, victim, ["vendor"])
  set_entity_meta(conn, survivor, "sector", "private")
  set_entity_meta(conn, victim, "region", "oslo")
  # relation victim -> other should become survivor -> other
  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, weight, suppress) VALUES (?,?,0.5,0)",
    (victim, other),
  )
  conn.commit()

  merge_entities(conn, survivor, [victim])
  assert set(get_entity_tags(conn, survivor)) == {"customer", "vendor"}
  assert get_entity_meta(conn, survivor) == {"sector": "private", "region": "oslo"}
  rel = conn.execute(
    "SELECT from_entity, to_entity FROM entity_relations"
  ).fetchall()
  assert [(r["from_entity"], r["to_entity"]) for r in rel] == [(survivor, other)]


def test_merge_drops_self_loop_relations():
  conn = _open_db()
  survivor = _ent(conn, "Crayon")
  victim = _ent(conn, "Crayon AS")
  # A relation between the two being merged would become a self-loop.
  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, weight, suppress) VALUES (?,?,0.5,0)",
    (survivor, victim),
  )
  conn.commit()
  merge_entities(conn, survivor, [victim])
  assert conn.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0] == 0


def test_merge_updates_promotion_candidates_by_name():
  conn = _open_db()
  survivor = _ent(conn, "Crayon")
  victim = _ent(conn, "Crayon AS")
  conn.execute(
    "INSERT INTO promotion_candidates (id, created_at, entity, draft_title, draft_statement, source_item_ids) "
    "VALUES ('c1', '2026-01-01', 'Crayon AS', 't', 's', '[]')"
  )
  conn.commit()
  merge_entities(conn, survivor, [victim])
  ent = conn.execute("SELECT entity FROM promotion_candidates WHERE id='c1'").fetchone()[0]
  assert ent == "Crayon"


def test_prune_denies_and_clears_links():
  conn = _open_db()
  junk = _ent(conn, "Best regards")
  keep = _ent(conn, "Crayon")
  _item(conn, "1", [junk, keep])
  add_entity_tags(conn, junk, ["noise"])

  stats = prune_entity(conn, junk)
  assert stats["item_links"] == 1
  assert conn.execute(
    "SELECT pending_review FROM entities WHERE id=?", (junk,)
  ).fetchone()[0] == 2
  assert conn.execute(
    "SELECT COUNT(*) FROM item_entities WHERE entity_id=?", (junk,)
  ).fetchone()[0] == 0
  assert get_entity_tags(conn, junk) == []
  # the entity row itself survives (so NER cannot revive it as a fresh candidate)
  assert resolve_entity_id(conn, "Best regards") == junk


def test_build_cooccurrence_excludes_denied_entities():
  conn = _open_db()
  a = _ent(conn, "Alpha")
  b = _ent(conn, "Beta")
  junk = _ent(conn, "Best regards")
  c = _ent(conn, "Gamma")
  for i in range(5):
    _item(conn, f"ab{i}", [a, b, junk])  # junk co-occurs with everything
  for i in range(5):
    _item(conn, f"c{i}", [c])

  prune_entity(conn, junk)  # deny the junk entity
  build_cooccurrence(conn)
  # Alpha should associate with Beta but never with the denied junk entity.
  assoc = resolve_associations(conn, [a])
  assert b in assoc
  assert junk not in assoc
