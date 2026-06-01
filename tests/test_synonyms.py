from __future__ import annotations

import json
import sqlite3

from yaams.retrieve.synonyms import expand_fts_tokens, load_synonym_groups
from yaams.schema import init_schema


def _db_with_entities(rows: list[tuple[str, list[str]]]) -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  for canonical, aliases in rows:
    conn.execute(
      "INSERT INTO entities (canonical_name, entity_type, aliases) VALUES (?, ?, ?)",
      (canonical, "org", json.dumps(aliases)),
    )
  conn.commit()
  return conn


def test_load_groups_maps_every_form_to_full_group():
  conn = _db_with_entities([("Norconsult", ["nc", "norco"])])
  groups = load_synonym_groups(conn)
  # canonical and every alias key into the same group
  assert set(groups["norconsult"]) == {"Norconsult", "nc", "norco"}
  assert groups["nc"] == groups["norconsult"]
  assert groups["norco"] == groups["norconsult"]


def test_load_groups_dedupes_case_variant_aliases():
  conn = _db_with_entities([("Norconsult", ["nc", "NC"])])
  groups = load_synonym_groups(conn)
  # "nc" and "NC" casefold to the same key -> one surface form kept.
  assert groups["nc"] == ["Norconsult", "nc"]


def test_load_groups_skips_entities_without_aliases():
  conn = _db_with_entities([("Lonely Org", [])])
  groups = load_synonym_groups(conn)
  # A lone canonical with nothing to expand to is omitted.
  assert "lonely org" not in groups


def test_load_groups_dedupes_case_insensitively():
  conn = _db_with_entities([("FDEP", ["fdep", "Fdep", "forsvarsdepartementet"])])
  groups = load_synonym_groups(conn)
  forms = groups["fdep"]
  lowered = [f.casefold() for f in forms]
  assert len(lowered) == len(set(lowered))
  assert "forsvarsdepartementet" in lowered


def test_load_groups_tolerates_bad_alias_json():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases) VALUES (?, ?, ?)",
    ("Broken", "org", "{not valid json"),
  )
  conn.commit()
  # Bad JSON -> no aliases -> entity contributes nothing, no crash.
  assert load_synonym_groups(conn) == {}


def test_expand_tokens_pulls_in_group_members():
  groups = {"nc": ["nc", "Norconsult"], "norconsult": ["nc", "Norconsult"]}
  assert expand_fts_tokens(["nc"], groups) == ["nc", "Norconsult"]


def test_expand_tokens_passes_through_unknown_tokens():
  groups = {"nc": ["nc", "Norconsult"]}
  assert expand_fts_tokens(["budget", "review"], groups) == ["budget", "review"]


def test_expand_tokens_dedupes_across_expansion():
  groups = {"nc": ["nc", "Norconsult"], "norconsult": ["nc", "Norconsult"]}
  # Both tokens belong to the same group; result must not repeat forms.
  assert expand_fts_tokens(["nc", "norconsult"], groups) == ["nc", "Norconsult"]


def test_expand_tokens_empty_groups_is_identity():
  assert expand_fts_tokens(["nc", "norconsult"], {}) == ["nc", "norconsult"]
