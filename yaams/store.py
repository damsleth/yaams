from __future__ import annotations

from array import array
from dataclasses import dataclass
import json
import sqlite3
from typing import Iterable, Sequence

from yaams.consolidate.session import Consolidation
from yaams.ingest.base import Item
from yaams.time import ensure_utc

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
  cursor = conn.execute(
    """
    INSERT OR IGNORE INTO items
      (id, source, source_id, timestamp, sender, recipients, content,
       subject, thread_id, lang, raw_metadata, ingested_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
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
    ),
  )
  return int(cursor.rowcount == 1)


def _replace_embedding(
  conn: sqlite3.Connection,
  item_id: str,
  embedding: object,
) -> None:
  conn.execute("DELETE FROM items_vec WHERE item_id = ?", (item_id,))
  conn.execute(
    "INSERT INTO items_vec (item_id, embedding) VALUES (?, ?)",
    (item_id, _embedding_to_blob(embedding)),
  )


def _replace_fts(conn: sqlite3.Connection, item: Item) -> None:
  conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item.id,))
  conn.execute(
    """
    INSERT INTO items_fts (item_id, content, subject, sender)
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
    return embedding.astype("float32").tobytes()
  if isinstance(embedding, bytes):
    return embedding
  return array("f", [float(value) for value in embedding]).tobytes()


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
  placeholders = ",".join("?" * len(item_ids))
  conn.execute(
    f"UPDATE items SET consolidated_into = ? WHERE id IN ({placeholders})",
    (consolidation_id, *item_ids),
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
      id_placeholders = ",".join("?" * len(ids))
      conn.execute(
        f"UPDATE items SET consolidated_into = NULL WHERE consolidated_into IN ({id_placeholders})",
        tuple(ids),
      )
      conn.execute(
        f"DELETE FROM consolidations_vec WHERE consolidation_id IN ({id_placeholders})",
        tuple(ids),
      )
      conn.execute(
        f"DELETE FROM consolidations_fts WHERE consolidation_id IN ({id_placeholders})",
        tuple(ids),
      )
      conn.execute(
        f"DELETE FROM consolidations WHERE id IN ({id_placeholders})",
        tuple(ids),
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
