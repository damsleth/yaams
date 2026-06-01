from __future__ import annotations

import sqlite3

from yaams.retrieve.metadata import entities_matching
from yaams.schema import init_schema
from yaams.store import (
  add_entity_tags,
  get_entity_meta,
  get_entity_tags,
  remove_entity_meta,
  remove_entity_tags,
  resolve_entity_id,
  set_entity_meta,
)


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _ent(conn, name: str, etype: str = "org") -> int:
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)", (name, etype)
  )
  conn.commit()
  return resolve_entity_id(conn, name)


def test_tags_crud_roundtrip():
  conn = _open_db()
  e = _ent(conn, "Norconsult")
  assert add_entity_tags(conn, e, ["Customer", "customer", "DEFENSE"]) == 2  # dedupe + lower
  assert get_entity_tags(conn, e) == ["customer", "defense"]
  assert remove_entity_tags(conn, e, ["defense"]) == 1
  assert get_entity_tags(conn, e) == ["customer"]


def test_meta_crud_roundtrip_and_overwrite():
  conn = _open_db()
  e = _ent(conn, "Norconsult")
  set_entity_meta(conn, e, "Sector", "public")
  set_entity_meta(conn, e, "region", "oslo")
  assert get_entity_meta(conn, e) == {"sector": "public", "region": "oslo"}
  set_entity_meta(conn, e, "sector", "private")  # overwrite
  assert get_entity_meta(conn, e)["sector"] == "private"
  assert remove_entity_meta(conn, e, ["region"]) == 1
  assert get_entity_meta(conn, e) == {"sector": "private"}


def test_entities_matching_ands_tags_and_meta():
  conn = _open_db()
  a = _ent(conn, "Alpha")
  b = _ent(conn, "Beta")
  c = _ent(conn, "Gamma")
  add_entity_tags(conn, a, ["customer"])
  set_entity_meta(conn, a, "sector", "public")
  add_entity_tags(conn, b, ["customer"])  # tag but wrong sector
  set_entity_meta(conn, b, "sector", "private")
  add_entity_tags(conn, c, ["partner"])

  assert set(entities_matching(conn, tags=["customer"])) == {"Alpha", "Beta"}
  # AND: customer + sector=public -> only Alpha
  assert entities_matching(conn, tags=["customer"], meta={"sector": "public"}) == ["Alpha"]
  assert entities_matching(conn, meta={"sector": "private"}) == ["Beta"]
  assert entities_matching(conn) == []  # no constraints -> empty


def test_entities_matching_case_insensitive():
  conn = _open_db()
  a = _ent(conn, "Alpha")
  add_entity_tags(conn, a, ["Customer"])
  assert entities_matching(conn, tags=["CUSTOMER"]) == ["Alpha"]


def test_entities_matching_fail_soft_on_premigration_db():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute(
    "CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "canonical_name TEXT NOT NULL UNIQUE, entity_type TEXT NOT NULL, "
    "aliases TEXT, pending_review INTEGER NOT NULL DEFAULT 0)"
  )
  conn.commit()
  # No entity_tags / entity_meta tables -> [] rather than crash.
  assert entities_matching(conn, tags=["customer"]) == []
