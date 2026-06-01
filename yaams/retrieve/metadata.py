"""Resolve entity metadata constraints (tags + key/value attributes) to the
set of entities that satisfy them.

Custom metadata is modeled on entities, not documents: a tag like ``customer``
or an attribute like ``sector=public`` is attached to a canonical entity. At
query time a ``--tag``/``--meta`` constraint therefore resolves to the set of
entities that carry it, and retrieval reuses the existing entity machinery -
the same set drives either a hard ``entity_filter`` (filter mode) or a score
multiply over matching documents (boost mode).

Multiple constraints are AND-ed: an entity must satisfy every tag and every
key/value pair to qualify. Fail soft (return []) if the metadata tables are
absent on a pre-v6 DB.
"""

from __future__ import annotations

import sqlite3


def entities_matching(
  conn: sqlite3.Connection,
  *,
  tags: list[str] | None = None,
  meta: dict[str, str] | None = None,
) -> list[str]:
  """Return canonical names of entities that carry ALL given tags and ALL
  given key/value attributes. Empty constraints return []. Tag and key
  matching is case-insensitive; attribute values match exactly."""
  tag_list = [t.strip().lower() for t in (tags or []) if t.strip()]
  meta_items = [(k.strip().lower(), v) for k, v in (meta or {}).items() if k.strip()]
  if not tag_list and not meta_items:
    return []

  # Each constraint contributes a subquery of entity_ids; intersect them.
  id_sets: list[set[int]] = []
  try:
    for tag in tag_list:
      rows = conn.execute(
        "SELECT entity_id FROM entity_tags WHERE tag = ?", (tag,)
      ).fetchall()
      id_sets.append({r[0] if not hasattr(r, "keys") else r["entity_id"] for r in rows})
    for key, value in meta_items:
      rows = conn.execute(
        "SELECT entity_id FROM entity_meta WHERE key = ? AND value = ?",
        (key, value),
      ).fetchall()
      id_sets.append({r[0] if not hasattr(r, "keys") else r["entity_id"] for r in rows})
  except sqlite3.OperationalError:
    # Pre-v6 DB without the metadata tables - nothing matches.
    return []

  if not id_sets:
    return []
  qualifying = set.intersection(*id_sets) if len(id_sets) > 1 else id_sets[0]
  if not qualifying:
    return []

  ph = ",".join("?" * len(qualifying))
  rows = conn.execute(
    f"SELECT canonical_name FROM entities WHERE id IN ({ph})",
    tuple(qualifying),
  ).fetchall()
  return [r[0] if not hasattr(r, "keys") else r["canonical_name"] for r in rows]
