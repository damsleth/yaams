from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from yaams.ingest.base import Item, hash_id
from yaams.retrieve.associate import (
  build_cooccurrence,
  expand_query_entities,
  resolve_associations,
)
from yaams.schema import init_schema
from yaams.store import store_items


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _add_entity(conn, name: str, etype: str = "org") -> int:
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
    (name, etype),
  )
  conn.commit()
  return conn.execute(
    "SELECT id FROM entities WHERE canonical_name = ?", (name,)
  ).fetchone()["id"]


def _add_item(conn, msg_id: str, entity_ids: list[int]) -> str:
  item = Item(
    id=hash_id("imessage", f"t:{msg_id}"),
    source="imessage",
    source_id=f"t:{msg_id}",
    timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=UTC) + timedelta(minutes=int(msg_id, 36) % 1000),
    sender="a@test",
    recipients=[],
    content=f"content {msg_id}",
    subject="",
    thread_id="t",
  )
  store_items(conn, [item], [b"\x00" * 16], [[]])
  for eid in entity_ids:
    conn.execute(
      "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, ?)",
      (item.id, eid, "test"),
    )
  conn.commit()
  return item.id


def test_build_cooccurrence_scores_strong_pair():
  conn = _open_db()
  a = _add_entity(conn, "Alpha")
  b = _add_entity(conn, "Beta")
  c = _add_entity(conn, "Gamma")
  # Alpha & Beta always appear together; Gamma is separate.
  for i in range(5):
    _add_item(conn, f"ab{i}", [a, b])
  for i in range(5):
    _add_item(conn, f"c{i}", [c])

  pairs = build_cooccurrence(conn)
  assert pairs == 1  # only (Alpha, Beta)
  assoc = resolve_associations(conn, [a])
  assert b in assoc
  assert c not in assoc
  assert assoc[b] > 0.9  # perfect co-occurrence -> NPMI ~ 1.0


def test_build_cooccurrence_respects_min_cooccur():
  conn = _open_db()
  a = _add_entity(conn, "Alpha")
  b = _add_entity(conn, "Beta")
  _add_item(conn, "x1", [a, b])  # co-occur only once
  pairs = build_cooccurrence(conn, min_cooccur=3)
  assert pairs == 0
  assert resolve_associations(conn, [a]) == {}


def test_resolve_manual_override_beats_learned():
  conn = _open_db()
  a = _add_entity(conn, "Alpha")
  b = _add_entity(conn, "Beta")
  c = _add_entity(conn, "Gamma")
  for i in range(5):
    _add_item(conn, f"ab{i}", [a, b])
  for i in range(5):  # filler so Alpha/Beta are not ubiquitous (PMI > 0)
    _add_item(conn, f"c{i}", [c])
  build_cooccurrence(conn)
  learned = resolve_associations(conn, [a])[b]
  assert learned > 0.9

  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, weight, suppress) "
    "VALUES (?, ?, ?, 0)",
    (a, b, 0.25),
  )
  conn.commit()
  assert resolve_associations(conn, [a])[b] == 0.25


def test_resolve_suppress_removes_learned_edge():
  conn = _open_db()
  a = _add_entity(conn, "Alpha")
  b = _add_entity(conn, "Beta")
  c = _add_entity(conn, "Gamma")
  for i in range(5):
    _add_item(conn, f"ab{i}", [a, b])
  for i in range(5):  # filler so the learned edge clears the PMI floor
    _add_item(conn, f"c{i}", [c])
  build_cooccurrence(conn)
  assert b in resolve_associations(conn, [a])

  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, weight, suppress) "
    "VALUES (?, ?, 0, 1)",
    (a, b),
  )
  conn.commit()
  assert b not in resolve_associations(conn, [a])


def test_resolve_manual_only_link_without_learned():
  conn = _open_db()
  a = _add_entity(conn, "fdep")
  b = _add_entity(conn, "langkaia")
  # No co-occurrence at all; a hand-authored relation still resolves.
  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, kind, weight, suppress) "
    "VALUES (?, ?, 'located_at', 0.8, 0)",
    (a, b),
  )
  conn.commit()
  assert resolve_associations(conn, [a]) == {b: 0.8}


def test_expand_query_entities_adds_weighted_names():
  conn = _open_db()
  a = _add_entity(conn, "fdep")
  b = _add_entity(conn, "langkaia")
  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, weight, suppress) "
    "VALUES (?, ?, 0.8, 0)",
    (a, b),
  )
  conn.commit()
  expanded, weights = expand_query_entities(conn, ["fdep"])
  assert set(expanded) == {"fdep", "langkaia"}
  assert weights["fdep"] == 1.0
  assert weights["langkaia"] == 0.8


def test_expand_query_entities_noop_when_no_associations():
  conn = _open_db()
  _add_entity(conn, "Lonely")
  expanded, weights = expand_query_entities(conn, ["Lonely"])
  assert expanded == ["Lonely"]
  assert weights == {"lonely": 1.0}
