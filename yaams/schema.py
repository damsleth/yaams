from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 4
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

      CREATE TABLE IF NOT EXISTS consolidations (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        start_timestamp TEXT NOT NULL,
        end_timestamp TEXT NOT NULL,
        participants TEXT NOT NULL,
        item_count INTEGER NOT NULL,
        summary TEXT NOT NULL,
        raw_item_ids TEXT NOT NULL,
        consolidator_version TEXT NOT NULL,
        created_at TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_cons_source ON consolidations(source);
      CREATE INDEX IF NOT EXISTS idx_cons_thread ON consolidations(thread_id);
      CREATE INDEX IF NOT EXISTS idx_cons_time ON consolidations(start_timestamp);

      CREATE VIRTUAL TABLE IF NOT EXISTS consolidations_fts USING fts5(
        consolidation_id UNINDEXED,
        summary,
        participants,
        tokenize = 'unicode61 remove_diacritics 2'
      );

      CREATE TABLE IF NOT EXISTS queries (
        id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        top_k INTEGER NOT NULL,
        source_filter TEXT,
        since TEXT,
        until TEXT,
        backend TEXT,
        model TEXT,
        latency_ms REAL,
        retrieval_ms REAL,
        synthesis_ms REAL,
        results_returned INTEGER NOT NULL DEFAULT 0,
        answer TEXT,
        ts TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_queries_ts ON queries(ts);

      CREATE TABLE IF NOT EXISTS query_results (
        query_id TEXT NOT NULL,
        rank INTEGER NOT NULL,
        result_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        source TEXT,
        rrf_score REAL,
        fts_rank INTEGER,
        vector_rank INTEGER,
        cited INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (query_id, rank),
        FOREIGN KEY (query_id) REFERENCES queries(id)
      );

      CREATE INDEX IF NOT EXISTS idx_query_results_result ON query_results(result_id);

      CREATE TABLE IF NOT EXISTS query_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        result_id TEXT,
        payload TEXT,
        ts TEXT NOT NULL,
        FOREIGN KEY (query_id) REFERENCES queries(id)
      );

      CREATE INDEX IF NOT EXISTS idx_query_feedback_query ON query_feedback(query_id);
      """
    )
    _migrate_items_consolidated_into(conn)
    _migrate_items_promoted_to(conn)
    _migrate_promotion_candidates(conn)
    _migrate_query_structured_fields(conn)
    _init_vector_table(conn, embedding_dim, vector_enabled)
    _init_consolidations_vec(conn, embedding_dim, vector_enabled)


def _migrate_query_structured_fields(conn: sqlite3.Connection) -> None:
  cols = {row[1] for row in conn.execute("PRAGMA table_info(queries)")}
  additions = (
    ("parsed_query", "TEXT"),
    ("shape", "TEXT"),
    ("confidence", "TEXT"),
    ("confidence_reason", "TEXT"),
    ("gaps", "TEXT"),
    ("parser_fallback", "INTEGER NOT NULL DEFAULT 0"),
  )
  for name, decl in additions:
    if name not in cols:
      conn.execute(f"ALTER TABLE queries ADD COLUMN {name} {decl}")


def _migrate_items_consolidated_into(conn: sqlite3.Connection) -> None:
  cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
  if "consolidated_into" in cols:
    return
  conn.execute("ALTER TABLE items ADD COLUMN consolidated_into TEXT")
  conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_items_consolidated ON items(consolidated_into)"
  )


def _migrate_items_promoted_to(conn: sqlite3.Connection) -> None:
  cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
  if "promoted_to" not in cols:
    conn.execute("ALTER TABLE items ADD COLUMN promoted_to TEXT")


def _migrate_promotion_candidates(conn: sqlite3.Connection) -> None:
  conn.execute(
    """
    CREATE TABLE IF NOT EXISTS promotion_candidates (
      id TEXT PRIMARY KEY,
      created_at TEXT NOT NULL,
      entity TEXT,
      draft_type TEXT,
      draft_title TEXT NOT NULL,
      draft_statement TEXT NOT NULL,
      draft_body TEXT,
      draft_tags TEXT,
      source_item_ids TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      reviewed_at TEXT,
      promoted_path TEXT,
      backend TEXT,
      model TEXT
    )
    """
  )
  conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_promo_status ON promotion_candidates(status)"
  )
  conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_promo_entity ON promotion_candidates(entity)"
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

