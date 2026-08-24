from __future__ import annotations

import json
import re
import sqlite3
from array import array
from dataclasses import dataclass
from typing import Iterable, Sequence, cast

from yaams.consolidate.session import Consolidation
from yaams.ingest.base import Item
from yaams.schema import FACTS_FTS_TABLE, FACTS_SOURCE, FACTS_VEC_TABLE
from yaams.time import ensure_utc
from yaams.trust import derive_provenance

EntityTag = tuple[str, str, float, str]


@dataclass(frozen=True)
class StoreStats:
  items_seen: int = 0
  items_inserted: int = 0
  entity_links_inserted: int = 0


def seed_entities(conn: sqlite3.Connection, dictionary: Iterable[dict]) -> None:
  with conn:
    for entry in dictionary:
      canonical = str(entry["canonical"])
      entity_type = str(entry.get("type", "other"))
      aliases = list(entry.get("aliases", []))
      aliases_json = json.dumps(aliases, ensure_ascii=False)
      existing = conn.execute(
        "SELECT id FROM entities WHERE lower(canonical_name) = lower(?)",
        (canonical,),
      ).fetchone()
      if existing is None:
        conn.execute(
          """
          INSERT INTO entities
            (canonical_name, entity_type, aliases, pending_review)
          VALUES (?, ?, ?, 0)
          """,
          (canonical, entity_type, aliases_json),
        )
      else:
        conn.execute(
          """
          UPDATE entities
          SET canonical_name = ?,
            entity_type = ?,
            aliases = ?,
            pending_review = 0
          WHERE id = ?
          """,
          (canonical, entity_type, aliases_json, existing["id"]),
        )


def resolve_entity_id(conn: sqlite3.Connection, name: str) -> int | None:
  row = conn.execute(
    "SELECT id FROM entities WHERE lower(canonical_name) = lower(?)",
    (name.strip(),),
  ).fetchone()
  if row is None:
    return None
  return row[0] if not hasattr(row, "keys") else row["id"]


def add_entity_tags(conn: sqlite3.Connection, entity_id: int, tags: Iterable[str]) -> int:
  """Attach lowercased membership tags to an entity. Idempotent. Returns the
  number of tags newly inserted."""
  added = 0
  with conn:
    for tag in tags:
      norm = tag.strip().lower()
      if not norm:
        continue
      cur = conn.execute(
        "INSERT OR IGNORE INTO entity_tags (entity_id, tag) VALUES (?, ?)",
        (entity_id, norm),
      )
      added += cur.rowcount
  return added


def remove_entity_tags(conn: sqlite3.Connection, entity_id: int, tags: Iterable[str]) -> int:
  removed = 0
  with conn:
    for tag in tags:
      norm = tag.strip().lower()
      if not norm:
        continue
      cur = conn.execute(
        "DELETE FROM entity_tags WHERE entity_id = ? AND tag = ?",
        (entity_id, norm),
      )
      removed += cur.rowcount
  return removed


def set_entity_meta(conn: sqlite3.Connection, entity_id: int, key: str, value: str) -> None:
  """Set a key/value attribute on an entity (lowercased key, verbatim value).
  Upserts: setting an existing key overwrites its value."""
  norm_key = key.strip().lower()
  with conn:
    conn.execute(
      """
      INSERT INTO entity_meta (entity_id, key, value) VALUES (?, ?, ?)
      ON CONFLICT(entity_id, key) DO UPDATE SET value = excluded.value
      """,
      (entity_id, norm_key, value),
    )


def remove_entity_meta(conn: sqlite3.Connection, entity_id: int, keys: Iterable[str]) -> int:
  removed = 0
  with conn:
    for key in keys:
      norm = key.strip().lower()
      if not norm:
        continue
      cur = conn.execute(
        "DELETE FROM entity_meta WHERE entity_id = ? AND key = ?",
        (entity_id, norm),
      )
      removed += cur.rowcount
  return removed


def get_entity_tags(conn: sqlite3.Connection, entity_id: int) -> list[str]:
  return [
    (r[0] if not hasattr(r, "keys") else r["tag"])
    for r in conn.execute(
      "SELECT tag FROM entity_tags WHERE entity_id = ? ORDER BY tag", (entity_id,)
    )
  ]


def get_entity_meta(conn: sqlite3.Connection, entity_id: int) -> dict[str, str]:
  return {
    (r[0] if not hasattr(r, "keys") else r["key"]): (r[1] if not hasattr(r, "keys") else r["value"])
    for r in conn.execute(
      "SELECT key, value FROM entity_meta WHERE entity_id = ? ORDER BY key", (entity_id,)
    )
  }


def merge_entities(
  conn: sqlite3.Connection,
  survivor_id: int,
  victim_ids: Iterable[int],
) -> dict[str, int]:
  """Repoint every reference from each victim entity to the survivor, then
  delete the victim rows. Transactional. Caller is responsible for first
  folding victim names/aliases into the survivor's config dictionary entry
  and reseeding, so the merge survives future NER re-tagging.

  Reassigns: item_entities (max-confidence on conflict), entity_tags,
  entity_meta (survivor's existing values win), entity_relations (dedupe +
  drop self-loops), promotion_candidates (matched by canonical name). Drops
  the victims' learned entity_assoc rows (rebuild with `assoc build`).
  """
  victims = [v for v in victim_ids if v != survivor_id]
  stats = {"victims": 0, "item_links": 0, "tags": 0, "meta": 0, "relations": 0}
  if not victims:
    return stats

  srow = conn.execute(
    "SELECT canonical_name FROM entities WHERE id = ?", (survivor_id,)
  ).fetchone()
  if srow is None:
    raise ValueError(f"survivor entity id {survivor_id} does not exist")
  survivor_name = srow[0] if not hasattr(srow, "keys") else srow["canonical_name"]

  with conn:
    for vid in victims:
      vrow = conn.execute(
        "SELECT canonical_name FROM entities WHERE id = ?", (vid,)
      ).fetchone()
      if vrow is None:
        continue
      vname = vrow[0] if not hasattr(vrow, "keys") else vrow["canonical_name"]

      stats["item_links"] += conn.execute(
        "SELECT COUNT(*) FROM item_entities WHERE entity_id = ?", (vid,)
      ).fetchone()[0]
      conn.execute(
        """
        INSERT INTO item_entities (item_id, entity_id, confidence, source)
        SELECT item_id, ?, confidence, source FROM item_entities WHERE entity_id = ?
        ON CONFLICT(item_id, entity_id)
          DO UPDATE SET confidence = MAX(item_entities.confidence, excluded.confidence)
        """,
        (survivor_id, vid),
      )
      conn.execute("DELETE FROM item_entities WHERE entity_id = ?", (vid,))

      conn.execute(
        "INSERT OR IGNORE INTO entity_tags (entity_id, tag) "
        "SELECT ?, tag FROM entity_tags WHERE entity_id = ?",
        (survivor_id, vid),
      )
      conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (vid,))

      conn.execute(
        "INSERT OR IGNORE INTO entity_meta (entity_id, key, value) "
        "SELECT ?, key, value FROM entity_meta WHERE entity_id = ?",
        (survivor_id, vid),
      )
      conn.execute("DELETE FROM entity_meta WHERE entity_id = ?", (vid,))

      conn.execute(
        "UPDATE OR IGNORE entity_relations SET from_entity = ? WHERE from_entity = ?",
        (survivor_id, vid),
      )
      conn.execute(
        "UPDATE OR IGNORE entity_relations SET to_entity = ? WHERE to_entity = ?",
        (survivor_id, vid),
      )
      conn.execute(
        "DELETE FROM entity_relations WHERE from_entity = ? OR to_entity = ?",
        (vid, vid),
      )
      conn.execute(
        "DELETE FROM entity_relations WHERE from_entity = to_entity"
      )

      conn.execute(
        "DELETE FROM entity_assoc WHERE entity_a = ? OR entity_b = ?", (vid, vid)
      )
      conn.execute(
        "UPDATE promotion_candidates SET entity = ? WHERE entity = ?",
        (survivor_name, vname),
      )
      conn.execute("DELETE FROM entities WHERE id = ?", (vid,))
      stats["victims"] += 1

  return stats


_EDGE_PUNCT = re.compile(r"^\W+|\W+$", re.UNICODE)


def canonical_norm(name: str) -> str:
  """Strict normalization for detecting punctuation-only entity variants:
  collapse internal whitespace and strip leading/trailing non-word characters.
  Case and internal punctuation are preserved (mirrors the NER normalizer's
  edge-stripping, minus the title-casing)."""
  collapsed = re.sub(r"\s+", " ", name).strip()
  return _EDGE_PUNCT.sub("", collapsed)


def normalize_entities(
  conn: sqlite3.Connection,
  *,
  dry_run: bool = False,
) -> dict:
  """Auto-merge entities that differ only by leading/trailing punctuation or
  whitespace (e.g. "Hamas" / "Hamas'", "`Saksnavn" / "Saksnavn`").

  Entities are grouped by their case-folded :func:`canonical_norm`. Within a
  group the survivor is the member already equal to the clean form, else the
  most-linked member is renamed to it; the rest are merged in. Denied
  entities are left alone. Durability comes from the NER normalizer now
  emitting clean forms, so this is a pure DB cleanup (no config promotion).

  Returns ``{"merged": n, "renamed": n, "groups": [{survivor, victims}]}``.
  With ``dry_run`` nothing is written; the planned groups are still returned.
  """
  rows = conn.execute(
    """
    SELECT e.id AS id, e.canonical_name AS name, COUNT(ie.item_id) AS cnt
    FROM entities e
    LEFT JOIN item_entities ie ON ie.entity_id = e.id
    WHERE e.pending_review != 2
    GROUP BY e.id
    """
  ).fetchall()

  groups: dict[str, list[tuple[int, str, int]]] = {}
  for row in rows:
    name = row["name"]
    norm = canonical_norm(name)
    if not norm:
      continue
    groups.setdefault(norm.casefold(), []).append((row["id"], name, int(row["cnt"])))

  merged = 0
  renamed = 0
  planned: list[dict] = []
  for members in groups.values():
    members.sort(key=lambda m: (-m[2], len(m[1])))
    clean = canonical_norm(members[0][1])
    if len(members) == 1 and members[0][1] == clean:
      continue  # already clean and alone — nothing to do

    survivor_id = next((eid for eid, name, _ in members if name == clean), None)
    member_ids = {m[0] for m in members}
    will_rename = False
    if survivor_id is None:
      existing = resolve_entity_id(conn, clean)
      if existing is not None and existing not in member_ids:
        survivor_id = existing
      else:
        survivor_id = members[0][0]
        will_rename = members[0][1] != clean

    victim_ids = [m[0] for m in members if m[0] != survivor_id]
    if not victim_ids and not will_rename:
      continue
    planned.append({
      "survivor": clean,
      "victims": [m[1] for m in members if m[0] != survivor_id],
    })
    if dry_run:
      continue

    if will_rename:
      try:
        with conn:
          conn.execute(
            "UPDATE entities SET canonical_name = ? WHERE id = ?",
            (clean, survivor_id),
          )
        renamed += 1
      except sqlite3.IntegrityError:
        # `clean` already exists as another entity — merge into it instead.
        survivor_id = resolve_entity_id(conn, clean) or survivor_id
        victim_ids = [m[0] for m in members if m[0] != survivor_id]
    if victim_ids:
      merge_entities(conn, survivor_id, victim_ids)
      merged += len(victim_ids)

  return {"merged": merged, "renamed": renamed, "groups": planned}


def prune_entity(conn: sqlite3.Connection, entity_id: int) -> dict[str, int]:
  """Mark an entity as denied (pending_review=2) and strip its derived data
  and links. Denial persists across re-ingest (the row is kept so NER's
  INSERT OR IGNORE cannot revive it as a fresh candidate), and the cleared
  links/associations keep junk out of retrieval until any future re-tag."""
  stats = {
    "item_links": conn.execute(
      "SELECT COUNT(*) FROM item_entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()[0],
  }
  with conn:
    conn.execute(
      "UPDATE entities SET pending_review = 2 WHERE id = ?", (entity_id,)
    )
    conn.execute("DELETE FROM item_entities WHERE entity_id = ?", (entity_id,))
    conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,))
    conn.execute("DELETE FROM entity_meta WHERE entity_id = ?", (entity_id,))
    conn.execute(
      "DELETE FROM entity_relations WHERE from_entity = ? OR to_entity = ?",
      (entity_id, entity_id),
    )
    conn.execute(
      "DELETE FROM entity_assoc WHERE entity_a = ? OR entity_b = ?",
      (entity_id, entity_id),
    )
  return stats


def vacuum_orphan_entities(
  conn: sqlite3.Connection,
  *,
  dry_run: bool = False,
) -> dict[str, int]:
  """Delete unreviewed NER entities (pending_review = 1) that nothing
  references: no item links, tags, meta, relations, associations, and no
  promotion candidate. These accumulate when a re-tag with a better model
  or stricter filters stops producing links for old junk rows. Curated
  (pending_review = 0) and denied (2) entities are never touched — denial
  must persist so junk cannot resurface as a discover candidate."""
  orphan_sql = """
    SELECT e.id FROM entities e
    WHERE e.pending_review = 1
      AND NOT EXISTS (SELECT 1 FROM item_entities ie WHERE ie.entity_id = e.id)
      AND NOT EXISTS (SELECT 1 FROM entity_tags t WHERE t.entity_id = e.id)
      AND NOT EXISTS (SELECT 1 FROM entity_meta m WHERE m.entity_id = e.id)
      AND NOT EXISTS (
        SELECT 1 FROM entity_relations r
        WHERE r.from_entity = e.id OR r.to_entity = e.id
      )
      AND NOT EXISTS (
        SELECT 1 FROM entity_assoc a
        WHERE a.entity_a = e.id OR a.entity_b = e.id
      )
      AND NOT EXISTS (
        SELECT 1 FROM promotion_candidates p
        WHERE lower(p.entity) = lower(e.canonical_name)
      )
  """
  count = conn.execute(
    f"SELECT COUNT(*) FROM ({orphan_sql})"
  ).fetchone()[0]
  if not dry_run and count:
    with conn:
      conn.execute(f"DELETE FROM entities WHERE id IN ({orphan_sql})")
  return {"deleted": 0 if dry_run else count, "orphans": count}


def backfill_entity_sources(conn: sqlite3.Connection, dictionary: Iterable[dict]) -> int:
  """Upgrade item_entities.source from 'ner' to 'dictionary' for entities now in the dictionary.

  When entities are added via discover/add after initial ingest, their existing
  item_entity links have source='ner'. This makes them invisible to the promotion
  pipeline which filters on source='dictionary'. Call this after seed_entities.
  """
  upgraded = 0
  with conn:
    for entry in dictionary:
      canonical = str(entry["canonical"])
      row = conn.execute(
        "SELECT id FROM entities WHERE lower(canonical_name) = lower(?)", (canonical,)
      ).fetchone()
      if row is None:
        continue
      cur = conn.execute(
        "UPDATE item_entities SET source = 'dictionary' WHERE entity_id = ? AND source = 'ner'",
        (row["id"],),
      )
      upgraded += cur.rowcount
  return upgraded


# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999; stay under it when an
# id list is built from a result set rather than a bounded batch.
# ponytail: itertools.batched would do this, but it lands in 3.12 and we support 3.11.
_SQL_VARS = 900


def _chunked(seq: Sequence[str], size: int = _SQL_VARS):
  for start in range(0, len(seq), size):
    yield seq[start:start + size]


def existing_ids(conn: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
  """Return the subset of ``ids`` already present in the items table.

  Lets the ingest loop drop already-stored items before the expensive
  embed/tag step — these sources are append-only (id = hash of source +
  source_id), so a known id has identical content and re-embedding is pure
  waste. When a run re-sees only known items, the embedding model is never
  loaded at all.
  """
  found: set[str] = set()
  for chunk in _chunked(list(ids)):
    placeholders = ",".join("?" * len(chunk))
    rows = conn.execute(
      f"SELECT id FROM items WHERE id IN ({placeholders})", chunk
    )
    found.update(row[0] for row in rows)
  return found


def store_items(
  conn: sqlite3.Connection,
  items: Sequence[Item],
  embeddings: Sequence[object],
  entity_tags: Sequence[Sequence[EntityTag]],
) -> StoreStats:
  if not (len(items) == len(embeddings) == len(entity_tags)):
    raise ValueError("items, embeddings, and entity_tags must have the same length")

  inserted = 0
  links = 0
  with conn:
    for item, embedding, tags in zip(items, embeddings, entity_tags):
      inserted += _insert_item(conn, item)
      # chats_facts is an isolated retrieval tier: its index rows live in
      # separate fts/vec tables so they never touch shared corpus statistics
      # (see schema._init_facts_indexes). The `items` row itself is normal, so
      # hydration + entity links are unchanged.
      if item.source == FACTS_SOURCE:
        _replace_embedding(conn, item.id, embedding, vec_table=FACTS_VEC_TABLE)
        _replace_fts(conn, item, fts_table=FACTS_FTS_TABLE)
      else:
        _replace_embedding(conn, item.id, embedding)
        _replace_fts(conn, item)
      links += _replace_entity_links(conn, item.id, tags)
  return StoreStats(
    items_seen=len(items),
    items_inserted=inserted,
    entity_links_inserted=links,
  )


def database_stats(conn: sqlite3.Connection) -> dict:
  by_source = {
    row["source"]: row["count"]
    for row in conn.execute(
      "SELECT source, count(*) AS count FROM items GROUP BY source ORDER BY source"
    )
  }
  total = conn.execute("SELECT count(*) AS count FROM items").fetchone()["count"]
  date_range = conn.execute(
    "SELECT min(timestamp) AS min_ts, max(timestamp) AS max_ts FROM items"
  ).fetchone()
  entities = conn.execute("SELECT count(*) AS count FROM entities").fetchone()["count"]
  entity_links = conn.execute(
    "SELECT count(*) AS count FROM item_entities"
  ).fetchone()["count"]
  return {
    "by_source": by_source,
    "total": total,
    "date_min": date_range["min_ts"],
    "date_max": date_range["max_ts"],
    "entities": entities,
    "entity_links": entity_links,
  }


def _insert_item(conn: sqlite3.Connection, item: Item) -> int:
  # UPDATE-on-exists refreshes derived fields for the SAME logical item only
  # (id = hash(source:source_id)). It must never overwrite the meaning of a
  # fact — mutable sources get a new id via revision-in-source_id. This is why
  # the raw store stays append-only. See AGENTS.md "Raw-store invariants".
  exists = conn.execute("SELECT 1 FROM items WHERE id = ?", (item.id,)).fetchone()
  # Origin-trust class for the item, derived from its ingest source. Stored so
  # trust verdicts (yaams.trust) need not re-derive it per query; legacy rows
  # written before this column fall back to derivation at query time.
  provenance = derive_provenance(
    item.source, timestamp_inferred=item.timestamp_inferred
  )
  params = (
    item.id,
    item.source,
    item.source_id,
    ensure_utc(item.timestamp).isoformat(),
    item.sender,
    json.dumps(item.recipients, ensure_ascii=False),
    item.content,
    item.subject,
    item.thread_id,
    item.lang,
    json.dumps(item.raw_metadata, ensure_ascii=False),
    ensure_utc(item.ingested_at).isoformat(),
    1 if item.timestamp_inferred else 0,
    provenance,
  )
  if exists is None:
    conn.execute(
      """
      INSERT INTO items
        (id, source, source_id, timestamp, sender, recipients, content,
         subject, thread_id, lang, raw_metadata, ingested_at, timestamp_inferred,
         provenance)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      params,
    )
    return 1
  conn.execute(
    """
    UPDATE items SET
      source = ?,
      source_id = ?,
      timestamp = ?,
      sender = ?,
      recipients = ?,
      content = ?,
      subject = ?,
      thread_id = ?,
      lang = ?,
      raw_metadata = ?,
      timestamp_inferred = ?,
      provenance = ?
    WHERE id = ?
    """,
    (
      item.source,
      item.source_id,
      ensure_utc(item.timestamp).isoformat(),
      item.sender,
      json.dumps(item.recipients, ensure_ascii=False),
      item.content,
      item.subject,
      item.thread_id,
      item.lang,
      json.dumps(item.raw_metadata, ensure_ascii=False),
      1 if item.timestamp_inferred else 0,
      provenance,
      item.id,
    ),
  )
  return 0


def _replace_embedding(
  conn: sqlite3.Connection,
  item_id: str,
  embedding: object,
  vec_table: str = "items_vec",
) -> None:
  conn.execute(f"DELETE FROM {vec_table} WHERE item_id = ?", (item_id,))
  conn.execute(
    f"INSERT INTO {vec_table} (item_id, embedding) VALUES (?, ?)",
    (item_id, _embedding_to_blob(embedding)),
  )


def _replace_fts(
  conn: sqlite3.Connection,
  item: Item,
  fts_table: str = "items_fts",
) -> None:
  conn.execute(f"DELETE FROM {fts_table} WHERE item_id = ?", (item.id,))
  conn.execute(
    f"""
    INSERT INTO {fts_table} (item_id, content, subject, sender)
    VALUES (?, ?, ?, ?)
    """,
    (item.id, item.content, item.subject or "", item.sender),
  )


def _replace_entity_links(
  conn: sqlite3.Connection,
  item_id: str,
  tags: Sequence[EntityTag],
) -> int:
  inserted = 0
  conn.execute("DELETE FROM item_entities WHERE item_id = ?", (item_id,))
  for canonical, entity_type, confidence, source in tags:
    entity_id = upsert_entity(conn, canonical, entity_type, source)
    cursor = conn.execute(
      """
      INSERT OR IGNORE INTO item_entities
        (item_id, entity_id, confidence, source)
      VALUES (?, ?, ?, ?)
      """,
      (item_id, entity_id, confidence, source),
    )
    inserted += int(cursor.rowcount == 1)
  return inserted


def upsert_entity(
  conn: sqlite3.Connection,
  canonical_name: str,
  entity_type: str,
  source: str,
) -> int:
  pending_review = 0 if source in {"dictionary", "manual"} else 1
  existing = conn.execute(
    "SELECT id, canonical_name FROM entities WHERE lower(canonical_name) = lower(?)",
    (canonical_name,),
  ).fetchone()
  if existing is not None:
    if pending_review == 0 and existing["canonical_name"] != canonical_name:
      conn.execute(
        "UPDATE entities SET canonical_name = ? WHERE id = ?",
        (canonical_name, existing["id"]),
      )
    canonical_name = canonical_name if pending_review == 0 else existing["canonical_name"]

  conn.execute(
    """
    INSERT OR IGNORE INTO entities
      (canonical_name, entity_type, aliases, pending_review)
    VALUES (?, ?, ?, ?)
    """,
    (canonical_name, entity_type, json.dumps([]), pending_review),
  )
  if pending_review == 0:
    conn.execute(
      """
      UPDATE entities
      SET entity_type = ?, pending_review = 0
      WHERE canonical_name = ?
      """,
      (entity_type, canonical_name),
    )
  row = conn.execute(
    "SELECT id FROM entities WHERE lower(canonical_name) = lower(?)",
    (canonical_name,),
  ).fetchone()
  if row is None:
    raise RuntimeError(f"Failed to upsert entity: {canonical_name}")
  return int(row["id"])


def _embedding_to_blob(embedding: object) -> bytes:
  if hasattr(embedding, "astype") and hasattr(embedding, "tobytes"):
    return embedding.astype("float32").tobytes()  # type: ignore[attr-defined]
  if isinstance(embedding, bytes):
    return embedding
  return array("f", [float(value) for value in cast(Iterable[float], embedding)]).tobytes()


def store_consolidations(
  conn: sqlite3.Connection,
  consolidations: Sequence[Consolidation],
  embeddings: Sequence[object] | None = None,
) -> int:
  if embeddings is not None and len(embeddings) != len(consolidations):
    raise ValueError("embeddings length must match consolidations length")
  inserted = 0
  with conn:
    for idx, consolidation in enumerate(consolidations):
      inserted += _insert_consolidation(conn, consolidation)
      _replace_consolidation_fts(conn, consolidation)
      if embeddings is not None:
        _replace_consolidation_embedding(conn, consolidation.id, embeddings[idx])
      _mark_items_consolidated(conn, consolidation.id, consolidation.raw_item_ids)
  return inserted


def _insert_consolidation(conn: sqlite3.Connection, consolidation: Consolidation) -> int:
  cursor = conn.execute(
    """
    INSERT OR REPLACE INTO consolidations (
      id, source, thread_id, start_timestamp, end_timestamp,
      participants, item_count, summary, raw_item_ids,
      consolidator_version, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      consolidation.id,
      consolidation.source,
      consolidation.thread_id,
      ensure_utc(consolidation.start_timestamp).isoformat(),
      ensure_utc(consolidation.end_timestamp).isoformat(),
      json.dumps(consolidation.participants, ensure_ascii=False),
      consolidation.item_count,
      consolidation.summary,
      json.dumps(consolidation.raw_item_ids, ensure_ascii=False),
      consolidation.consolidator_version,
      ensure_utc(consolidation.created_at).isoformat(),
    ),
  )
  return int(cursor.rowcount == 1)


def _replace_consolidation_fts(conn: sqlite3.Connection, consolidation: Consolidation) -> None:
  conn.execute(
    "DELETE FROM consolidations_fts WHERE consolidation_id = ?", (consolidation.id,)
  )
  conn.execute(
    "INSERT INTO consolidations_fts (consolidation_id, summary, participants) VALUES (?, ?, ?)",
    (
      consolidation.id,
      consolidation.summary,
      " ".join(consolidation.participants),
    ),
  )


def _replace_consolidation_embedding(
  conn: sqlite3.Connection,
  consolidation_id: str,
  embedding: object,
) -> None:
  conn.execute(
    "DELETE FROM consolidations_vec WHERE consolidation_id = ?", (consolidation_id,)
  )
  conn.execute(
    "INSERT INTO consolidations_vec (consolidation_id, embedding) VALUES (?, ?)",
    (consolidation_id, _embedding_to_blob(embedding)),
  )


def _mark_items_consolidated(
  conn: sqlite3.Connection,
  consolidation_id: str,
  item_ids: Sequence[str],
) -> None:
  if not item_ids:
    return
  for chunk in _chunked(list(item_ids)):
    placeholders = ",".join("?" * len(chunk))
    conn.execute(
      f"UPDATE items SET consolidated_into = ? WHERE id IN ({placeholders})",
      (consolidation_id, *chunk),
    )


def clear_consolidations(conn: sqlite3.Connection, sources: Sequence[str] | None = None) -> int:
  with conn:
    if sources:
      placeholders = ",".join("?" * len(sources))
      ids = [
        row[0]
        for row in conn.execute(
          f"SELECT id FROM consolidations WHERE source IN ({placeholders})",
          tuple(sources),
        )
      ]
      if not ids:
        return 0
      for chunk in _chunked(ids):
        id_placeholders = ",".join("?" * len(chunk))
        conn.execute(
          f"UPDATE items SET consolidated_into = NULL "
          f"WHERE consolidated_into IN ({id_placeholders})",
          tuple(chunk),
        )
        conn.execute(
          f"DELETE FROM consolidations_vec WHERE consolidation_id IN ({id_placeholders})",
          tuple(chunk),
        )
        conn.execute(
          f"DELETE FROM consolidations_fts WHERE consolidation_id IN ({id_placeholders})",
          tuple(chunk),
        )
        conn.execute(
          f"DELETE FROM consolidations WHERE id IN ({id_placeholders})",
          tuple(chunk),
        )
      return len(ids)
    count = conn.execute("SELECT count(*) FROM consolidations").fetchone()[0]
    conn.execute("UPDATE items SET consolidated_into = NULL")
    conn.execute("DELETE FROM consolidations_vec")
    conn.execute("DELETE FROM consolidations_fts")
    conn.execute("DELETE FROM consolidations")
    return int(count)


def fetch_items_for_consolidation(
  conn: sqlite3.Connection,
  source: str,
  since: str | None = None,
  only_unconsolidated: bool = True,
) -> list[Item]:
  query = """
    SELECT id, source, source_id, timestamp, sender, recipients,
           content, subject, thread_id, lang, raw_metadata, ingested_at
    FROM items
    WHERE source = ?
  """
  params: list[object] = [source]
  if since:
    query += " AND timestamp >= ?"
    params.append(since)
  if only_unconsolidated:
    query += " AND consolidated_into IS NULL"
  query += " ORDER BY thread_id, timestamp"

  out: list[Item] = []
  for row in conn.execute(query, tuple(params)):
    out.append(
      Item(
        id=row["id"],
        source=row["source"],
        source_id=row["source_id"],
        timestamp=ensure_utc(_parse_iso(row["timestamp"])),
        sender=row["sender"],
        recipients=json.loads(row["recipients"] or "[]"),
        content=row["content"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        raw_metadata=json.loads(row["raw_metadata"] or "{}") if row["raw_metadata"] else None,
      )
    )
  return out


def _parse_iso(value: str):
  from datetime import datetime
  return datetime.fromisoformat(value.replace("Z", "+00:00"))


def consolidation_stats(conn: sqlite3.Connection) -> dict:
  by_source: dict[str, dict[str, int]] = {}
  for row in conn.execute(
    "SELECT source, count(*) AS n, sum(item_count) AS items FROM consolidations GROUP BY source"
  ):
    by_source[row["source"]] = {
      "consolidations": int(row["n"] or 0),
      "items_consolidated": int(row["items"] or 0),
    }
  total_consolidations = conn.execute(
    "SELECT count(*) FROM consolidations"
  ).fetchone()[0]
  total_items_consolidated = conn.execute(
    "SELECT count(*) FROM items WHERE consolidated_into IS NOT NULL"
  ).fetchone()[0]
  return {
    "by_source": by_source,
    "total_consolidations": int(total_consolidations or 0),
    "total_items_consolidated": int(total_items_consolidated or 0),
  }
