from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 1
DEFAULT_EMBEDDING_DIM = 1024


def has_sqlite_vec(conn: sqlite3.Connection) -> bool:
  row = conn.execute(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'items_vec'"
  ).fetchone()
  if row is None:
    return False
  sql = conn.execute(
    "SELECT sql FROM sqlite_master WHERE name = 'items_vec'"
  ).fetchone()
  return bool(sql and "vec0" in (sql[0] or "").lower())


def init_schema(
  conn: sqlite3.Connection,
  embedding_dim: int = DEFAULT_EMBEDDING_DIM,
  use_vec: bool | None = None,
) -> None:
  vector_enabled = _can_create_vec(conn) if use_vec is None else use_vec
  with conn:
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.executescript(
      """
      CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        sender TEXT NOT NULL,
        recipients TEXT NOT NULL,
        content TEXT NOT NULL,
        subject TEXT,
        thread_id TEXT,
        lang TEXT,
        raw_metadata TEXT,
        ingested_at TEXT NOT NULL,
        UNIQUE (source, source_id)
      );

      CREATE INDEX IF NOT EXISTS idx_items_timestamp ON items(timestamp);
      CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
      CREATE INDEX IF NOT EXISTS idx_items_sender ON items(sender);
      CREATE INDEX IF NOT EXISTS idx_items_thread ON items(thread_id);

      CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        item_id UNINDEXED,
        content,
        subject,
        sender,
        tokenize = 'unicode61 remove_diacritics 2'
      );

      CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL,
        aliases TEXT,
        pending_review INTEGER NOT NULL DEFAULT 0
      );

      CREATE TABLE IF NOT EXISTS item_entities (
        item_id TEXT,
        entity_id INTEGER,
        confidence REAL DEFAULT 1.0,
        source TEXT,
        PRIMARY KEY (item_id, entity_id),
        FOREIGN KEY (item_id) REFERENCES items(id),
        FOREIGN KEY (entity_id) REFERENCES entities(id)
      );

      CREATE INDEX IF NOT EXISTS idx_item_entities_entity
        ON item_entities(entity_id);

      CREATE TABLE IF NOT EXISTS watermarks (
        source TEXT PRIMARY KEY,
        last_ingested_at TEXT NOT NULL,
        last_run_at TEXT NOT NULL
      );
      """
    )
    _init_vector_table(conn, embedding_dim, vector_enabled)


def _init_vector_table(
  conn: sqlite3.Connection,
  embedding_dim: int,
  vector_enabled: bool,
) -> None:
  existing = conn.execute(
    "SELECT sql FROM sqlite_master WHERE name = 'items_vec'"
  ).fetchone()
  if existing:
    return
  if vector_enabled:
    conn.execute(
      f"""
      CREATE VIRTUAL TABLE items_vec USING vec0(
        item_id TEXT PRIMARY KEY,
        embedding FLOAT[{embedding_dim}]
      )
      """
    )
    return
  conn.execute(
    """
    CREATE TABLE items_vec (
      item_id TEXT PRIMARY KEY,
      embedding BLOB NOT NULL
    )
    """
  )


def _can_create_vec(conn: sqlite3.Connection) -> bool:
  try:
    conn.execute("CREATE VIRTUAL TABLE temp._yaams_vec_probe USING vec0(v FLOAT[1])")
    conn.execute("DROP TABLE temp._yaams_vec_probe")
    return True
  except sqlite3.DatabaseError:
    return False

