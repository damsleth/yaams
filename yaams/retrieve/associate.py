"""Entity associations: learned co-occurrence + manual overrides.

Two entities that travel together (e.g. "fdep" / Forsvarsdepartementet and
its physical location "langkaia") are NOT synonyms - they are distinct things
that are contextually related. Synonyms want substitution (handled in
``synonyms.py``); associations want *soft expansion with down-weighting*:
surface the related material, but never let it outrank the thing actually
asked about.

This module computes a learned association strength from how often entities
co-occur in the same items (normalized PMI, so ubiquitous entities like the
user themselves do not dominate), and merges in a hand-curated override table
(``entity_relations``) that can add, re-weight, or suppress specific links.

The builder is a maintenance pass (like consolidation); ``resolve_associations``
is the query-time lookup the retrieval layer uses.
"""

from __future__ import annotations

import math
import sqlite3

# A pair seen fewer than this many times is too sparse to trust.
MIN_COOCCUR = 3
# Normalized-PMI floor for a learned edge to be stored / used.
MIN_SCORE = 0.15


def build_cooccurrence(
  conn: sqlite3.Connection,
  *,
  min_cooccur: int = MIN_COOCCUR,
  min_score: float = MIN_SCORE,
) -> int:
  """Recompute the learned ``entity_assoc`` table from ``item_entities``.

  Uses normalized pointwise mutual information (NPMI) so a pair that always
  appears together scores ~1.0 while a pair that co-occurs only as often as
  chance scores ~0.0. Writes both directed rows per pair. Replaces the whole
  table. Returns the number of unordered pairs stored.
  """
  # Denied entities (pending_review = 2) are excluded everywhere so pruned
  # junk does not pollute co-occurrence.
  total = conn.execute(
    """
    SELECT COUNT(DISTINCT ie.item_id) FROM item_entities ie
    JOIN entities e ON e.id = ie.entity_id AND e.pending_review != 2
    """
  ).fetchone()[0]
  if not total:
    with conn:
      conn.execute("DELETE FROM entity_assoc")
    return 0

  counts = {
    row[0]: row[1]
    for row in conn.execute(
      """
      SELECT ie.entity_id, COUNT(DISTINCT ie.item_id) FROM item_entities ie
      JOIN entities e ON e.id = ie.entity_id AND e.pending_review != 2
      GROUP BY ie.entity_id
      """
    )
  }

  pairs = conn.execute(
    """
    SELECT a.entity_id AS a, b.entity_id AS b, COUNT(*) AS c
    FROM item_entities a
    JOIN item_entities b
      ON a.item_id = b.item_id AND a.entity_id < b.entity_id
    JOIN entities ea ON ea.id = a.entity_id AND ea.pending_review != 2
    JOIN entities eb ON eb.id = b.entity_id AND eb.pending_review != 2
    GROUP BY a.entity_id, b.entity_id
    HAVING c >= ?
    """,
    (min_cooccur,),
  ).fetchall()

  rows: list[tuple[int, int, float, int]] = []
  for a, b, c in pairs:
    ca, cb = counts.get(a, 0), counts.get(b, 0)
    if ca == 0 or cb == 0:
      continue
    p_ab = c / total
    pmi = math.log(p_ab / ((ca / total) * (cb / total)))
    if pmi <= 0:
      continue
    npmi = pmi / -math.log(p_ab) if p_ab < 1.0 else 1.0
    score = max(0.0, min(1.0, npmi))
    if score < min_score:
      continue
    rows.append((a, b, score, c))
    rows.append((b, a, score, c))

  with conn:
    conn.execute("DELETE FROM entity_assoc")
    conn.executemany(
      "INSERT INTO entity_assoc (entity_a, entity_b, score, cooccur) "
      "VALUES (?, ?, ?, ?)",
      rows,
    )
  return len(rows) // 2


def resolve_associations(
  conn: sqlite3.Connection,
  entity_ids: list[int],
  *,
  min_score: float = MIN_SCORE,
) -> dict[int, float]:
  """Return {associated_entity_id: weight in (0, 1]} for the given query
  entities, merging learned co-occurrence with manual overrides.

  Manual ``entity_relations`` win over learned scores: a non-suppressing row
  sets the weight outright; a suppressing row removes the target entirely
  (blocking any learned edge). Query entities never associate to themselves.
  When several query entities point at the same target, the strongest weight
  wins. Weights are clamped to (0, 1] so an associated doc can be lifted but
  never outweigh an exact-entity match.
  """
  if not entity_ids:
    return {}
  query_set = set(entity_ids)
  placeholders = ",".join("?" * len(entity_ids))

  weights: dict[int, float] = {}
  try:
    learned = conn.execute(
      f"SELECT entity_b, score FROM entity_assoc WHERE entity_a IN ({placeholders})",
      tuple(entity_ids),
    ).fetchall()
  except sqlite3.OperationalError:
    # Pre-v5 DB not yet migrated (table absent) - fail soft, no learned edges.
    learned = []
  for row in learned:
    target, score = row[0], float(row[1])
    if target in query_set or score < min_score:
      continue
    capped = max(0.0, min(1.0, score))
    if capped > weights.get(target, 0.0):
      weights[target] = capped

  # Manual overrides: suppress wins absolutely; otherwise set the weight.
  suppressed: set[int] = set()
  manual: dict[int, float] = {}
  try:
    relations = conn.execute(
      f"SELECT to_entity, weight, suppress FROM entity_relations "
      f"WHERE from_entity IN ({placeholders})",
      tuple(entity_ids),
    ).fetchall()
  except sqlite3.OperationalError:
    relations = []
  for row in relations:
    target, weight, suppress = row[0], float(row[1]), int(row[2])
    if target in query_set:
      continue
    if suppress:
      suppressed.add(target)
      continue
    capped = max(0.0, min(1.0, weight))
    if capped > manual.get(target, 0.0):
      manual[target] = capped

  for target in suppressed:
    weights.pop(target, None)
  for target, weight in manual.items():
    weights[target] = weight  # manual wins over learned

  return {t: w for t, w in weights.items() if w > 0.0}


def expand_query_entities(
  conn: sqlite3.Connection,
  query_names: list[str],
  *,
  min_score: float = MIN_SCORE,
) -> tuple[list[str], dict[str, float]]:
  """Expand a query's canonical entity names with their associations.

  Returns ``(expanded_names, weights)`` where ``expanded_names`` is the query
  entities plus every associated entity (the widened allowlist), and
  ``weights`` maps each lowercased canonical name to a score in (0, 1]: query
  entities at 1.0, associated entities at their merged learned/manual weight.

  Returns the inputs unchanged with all-1.0 weights when nothing associates,
  so the caller can apply it unconditionally.
  """
  base_weights = {n.lower(): 1.0 for n in query_names if n}
  if not query_names:
    return list(query_names), base_weights

  lowered = list(base_weights.keys())
  ph = ",".join("?" * len(lowered))
  id_to_name: dict[int, str] = {}
  name_to_id: dict[str, int] = {}
  for row in conn.execute(
    f"SELECT id, canonical_name FROM entities WHERE lower(canonical_name) IN ({ph})",
    tuple(lowered),
  ):
    eid = row[0] if not hasattr(row, "keys") else row["id"]
    name = row[1] if not hasattr(row, "keys") else row["canonical_name"]
    id_to_name[eid] = name
    name_to_id[name.lower()] = eid
  query_ids = list(id_to_name.keys())
  if not query_ids:
    return list(query_names), base_weights

  assoc = resolve_associations(conn, query_ids, min_score=min_score)
  if not assoc:
    return list(query_names), base_weights

  # Resolve associated ids back to canonical names.
  assoc_ph = ",".join("?" * len(assoc))
  weights = dict(base_weights)
  expanded = list(query_names)
  for row in conn.execute(
    f"SELECT id, canonical_name FROM entities WHERE id IN ({assoc_ph})",
    tuple(assoc.keys()),
  ):
    eid = row[0] if not hasattr(row, "keys") else row["id"]
    name = row[1] if not hasattr(row, "keys") else row["canonical_name"]
    key = name.lower()
    if key in weights:
      continue  # never down-weight a query entity
    weights[key] = assoc[eid]
    expanded.append(name)
  return expanded, weights
