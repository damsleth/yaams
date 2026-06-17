from __future__ import annotations

import sqlite3

# Informational only; schema_migrations table is the source of truth
SCHEMA_VERSION = 7
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
  from yaams.migrations import apply_pending

  vector_enabled = _can_create_vec(conn) if use_vec is None else use_vec
  apply_pending(conn)
  _init_vector_table(conn, embedding_dim, vector_enabled)
  _init_consolidations_vec(conn, embedding_dim, vector_enabled)


def _init_consolidations_vec(
  conn: sqlite3.Connection,
  embedding_dim: int,
  vector_enabled: bool,
) -> None:
  existing = conn.execute(
    "SELECT sql FROM sqlite_master WHERE name = 'consolidations_vec'"
  ).fetchone()
  if existing:
    return
  if vector_enabled:
    conn.execute(
      f"""
      CREATE VIRTUAL TABLE consolidations_vec USING vec0(
        consolidation_id TEXT PRIMARY KEY,
        embedding FLOAT[{embedding_dim}]
      )
      """
    )
    return
  conn.execute(
    """
    CREATE TABLE consolidations_vec (
      consolidation_id TEXT PRIMARY KEY,
      embedding BLOB NOT NULL
    )
    """
  )


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
