from __future__ import annotations

import sqlite3

# Informational only; schema_migrations table is the source of truth
SCHEMA_VERSION = 7
DEFAULT_EMBEDDING_DIM = 1024


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
  _init_facts_indexes(conn, embedding_dim, vector_enabled)


# `chats_facts` is an opt-in retrieval tier: atomic bullets extracted from chat
# summaries. It gets its OWN fts + vec tables (not the shared items_fts/
# items_vec) because merely indexing 600+ short facts alongside everything else
# shifts BM25 corpus statistics (IDF) and perturbs unrelated results — measured
# as a ~0.5% dev-fitness regression even when the facts were filtered from
# output. Separate indexes keep default retrieval byte-identical to a
# facts-free corpus; the tier is searched only on explicit `--source
# chats_facts`. Same column shapes as items_fts/items_vec so the shared search
# SQL works unchanged against them (see retrieve.hybrid table params).
FACTS_SOURCE = "chats_facts"
FACTS_FTS_TABLE = "chats_facts_fts"
FACTS_VEC_TABLE = "chats_facts_vec"


def _init_facts_indexes(
  conn: sqlite3.Connection,
  embedding_dim: int,
  vector_enabled: bool,
) -> None:
  conn.execute(
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS {FACTS_FTS_TABLE} USING fts5(
      item_id UNINDEXED,
      content,
      subject,
      sender,
      tokenize = 'unicode61 remove_diacritics 2'
    )
    """
  )
  existing = conn.execute(
    "SELECT sql FROM sqlite_master WHERE name = ?", (FACTS_VEC_TABLE,)
  ).fetchone()
  if existing:
    return
  if vector_enabled:
    conn.execute(
      f"""
      CREATE VIRTUAL TABLE {FACTS_VEC_TABLE} USING vec0(
        item_id TEXT PRIMARY KEY,
        embedding FLOAT[{embedding_dim}]
      )
      """
    )
    return
  conn.execute(
    f"""
    CREATE TABLE {FACTS_VEC_TABLE} (
      item_id TEXT PRIMARY KEY,
      embedding BLOB NOT NULL
    )
    """
  )


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
