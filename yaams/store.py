from __future__ import annotations

from array import array
from dataclasses import dataclass
import json
import sqlite3
from typing import Iterable, Sequence

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
      conn.execute(
        """
        INSERT INTO entities (canonical_name, entity_type, aliases, pending_review)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(canonical_name) DO UPDATE SET
          entity_type = excluded.entity_type,
          aliases = excluded.aliases,
          pending_review = 0
        """,
        (canonical, entity_type, json.dumps(aliases, ensure_ascii=False)),
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
    "SELECT id FROM entities WHERE canonical_name = ?",
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

