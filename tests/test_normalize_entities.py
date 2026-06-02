from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from yaams.enrich.entities import normalize_ner_canonical
from yaams.ingest.base import Item, hash_id
from yaams.schema import init_schema
from yaams.store import (
  canonical_norm,
  normalize_entities,
  resolve_entity_id,
  store_items,
)


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _ent(conn, name, etype="org"):
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
    "VALUES (?, ?, '[]', 1)",
    (name, etype),
  )
  conn.commit()
  return resolve_entity_id(conn, name)


def _link(conn, eid, msg_id):
  it = Item(
    id=hash_id("imessage", f"t:{msg_id}"), source="imessage", source_id=f"t:{msg_id}",
    timestamp=datetime(2026, 4, 1, tzinfo=UTC), sender="a", recipients=[],
    content="x", subject="", thread_id="t",
  )
  store_items(conn, [it], [b"\x00" * 16], [[]])
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, confidence, source) VALUES (?,?,0.7,'ner')",
    (it.id, eid),
  )
  conn.commit()


# --- source-level normalization -------------------------------------------

def test_ner_normalizer_strips_edge_punctuation():
  assert normalize_ner_canonical("Hamas'", "org") == "Hamas"
  assert normalize_ner_canonical("`Saksnavn", "org") == "Saksnavn"
  assert normalize_ner_canonical("Saksnavn`", "org") == "Saksnavn"
  assert normalize_ner_canonical("Title`", "org") == "Title"
  assert normalize_ner_canonical("Carl Joakim,‎", "person") == "Carl Joakim"


def test_ner_normalizer_preserves_internal_punctuation():
  assert normalize_ner_canonical("O'Brien", "person") == "O'Brien"
  assert normalize_ner_canonical("AT&T", "org") == "AT&T"


def test_canonical_norm_matches_edge_stripping():
  assert canonical_norm("Hamas'") == "Hamas"
  assert canonical_norm("  `Saksnavn`  ") == "Saksnavn"
  assert canonical_norm("O'Brien") == "O'Brien"


# --- DB-level auto-merge ---------------------------------------------------

def test_normalize_merges_punctuation_variant_into_clean():
  conn = _open_db()
  clean = _ent(conn, "Hamas")
  dirty = _ent(conn, "Hamas'")
  _link(conn, clean, "1")
  _link(conn, dirty, "2")

  result = normalize_entities(conn)
  assert result["merged"] == 1
  assert resolve_entity_id(conn, "Hamas'") is None  # variant gone
  assert resolve_entity_id(conn, "Hamas") == clean
  # both item links now hang off the clean survivor
  assert conn.execute(
    "SELECT COUNT(*) FROM item_entities WHERE entity_id=?", (clean,)
  ).fetchone()[0] == 2


def test_normalize_renames_lone_dirty_entity():
  conn = _open_db()
  dirty = _ent(conn, "Saksnavn`")
  _link(conn, dirty, "1")
  result = normalize_entities(conn)
  assert result["renamed"] == 1
  assert resolve_entity_id(conn, "Saksnavn") == dirty  # same row, clean name
  assert resolve_entity_id(conn, "Saksnavn`") is None


def test_normalize_picks_most_linked_as_survivor():
  conn = _open_db()
  few = _ent(conn, "Title`")
  many = _ent(conn, "Title")
  _link(conn, many, "1")
  _link(conn, many, "2")
  _link(conn, few, "3")
  normalize_entities(conn)
  # "Title" had more links and is already clean -> survivor
  assert resolve_entity_id(conn, "Title") == many
  assert resolve_entity_id(conn, "Title`") is None


def test_normalize_dry_run_changes_nothing():
  conn = _open_db()
  _ent(conn, "Hamas")
  _ent(conn, "Hamas'")
  result = normalize_entities(conn, dry_run=True)
  assert len(result["groups"]) == 1
  assert result["merged"] == 0
  assert resolve_entity_id(conn, "Hamas'") is not None  # still there


def test_normalize_leaves_distinct_entities_alone():
  conn = _open_db()
  _ent(conn, "Crayon")
  _ent(conn, "Crayon AS")  # token difference, NOT punctuation-only
  result = normalize_entities(conn)
  assert result["merged"] == 0
  assert resolve_entity_id(conn, "Crayon AS") is not None
