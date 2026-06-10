"""Baseline migration: initial schema up to user_version 4.

This migration applies the full DDL for fresh databases.
Existing databases at user_version=4 are stamped without re-running this.
"""
from __future__ import annotations

import sqlite3

name = "0001_baseline"
description = "Initial schema from init_schema up to user_version 4"


def apply(conn: sqlite3.Connection) -> None:
    """Create all baseline tables and indexes.

    This is the full DDL that was present in schema.py's executescript block
    plus the _migrate_* functions for columns that existed at user_version=4.
    For FRESH databases only -- existing DBs are stamped, not re-run.
    """
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
          timestamp_inferred INTEGER NOT NULL DEFAULT 0,
          consolidated_into TEXT,
          promoted_to TEXT,
          UNIQUE (source, source_id)
        );

        CREATE INDEX IF NOT EXISTS idx_items_timestamp ON items(timestamp);
        CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
        CREATE INDEX IF NOT EXISTS idx_items_sender ON items(sender);
        CREATE INDEX IF NOT EXISTS idx_items_thread ON items(thread_id);
        CREATE INDEX IF NOT EXISTS idx_items_consolidated ON items(consolidated_into);

        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
          item_id UNINDEXED,
          content,
          subject,
          sender,
          tokenize = 'unicode61 remove_diacritics 0'
        );

        CREATE TABLE IF NOT EXISTS entities (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          canonical_name TEXT NOT NULL UNIQUE,
          entity_type TEXT NOT NULL,
          aliases TEXT,
          pending_review INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_entities_canonical_lower
          ON entities(lower(canonical_name));

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

        CREATE TABLE IF NOT EXISTS entity_assoc (
          entity_a INTEGER NOT NULL,
          entity_b INTEGER NOT NULL,
          score REAL NOT NULL,
          cooccur INTEGER NOT NULL,
          PRIMARY KEY (entity_a, entity_b),
          FOREIGN KEY (entity_a) REFERENCES entities(id),
          FOREIGN KEY (entity_b) REFERENCES entities(id)
        );

        CREATE INDEX IF NOT EXISTS idx_entity_assoc_a ON entity_assoc(entity_a);

        CREATE TABLE IF NOT EXISTS entity_relations (
          from_entity INTEGER NOT NULL,
          to_entity INTEGER NOT NULL,
          kind TEXT,
          weight REAL NOT NULL DEFAULT 1.0,
          suppress INTEGER NOT NULL DEFAULT 0,
          created_at TEXT,
          PRIMARY KEY (from_entity, to_entity),
          FOREIGN KEY (from_entity) REFERENCES entities(id),
          FOREIGN KEY (to_entity) REFERENCES entities(id)
        );

        CREATE INDEX IF NOT EXISTS idx_entity_relations_from
          ON entity_relations(from_entity);

        CREATE TABLE IF NOT EXISTS entity_tags (
          entity_id INTEGER NOT NULL,
          tag TEXT NOT NULL,
          PRIMARY KEY (entity_id, tag),
          FOREIGN KEY (entity_id) REFERENCES entities(id)
        );

        CREATE INDEX IF NOT EXISTS idx_entity_tags_tag ON entity_tags(tag);

        CREATE TABLE IF NOT EXISTS entity_meta (
          entity_id INTEGER NOT NULL,
          key TEXT NOT NULL,
          value TEXT NOT NULL,
          PRIMARY KEY (entity_id, key),
          FOREIGN KEY (entity_id) REFERENCES entities(id)
        );

        CREATE INDEX IF NOT EXISTS idx_entity_meta_kv ON entity_meta(key, value);

        CREATE TABLE IF NOT EXISTS watermarks (
          source TEXT PRIMARY KEY,
          last_ingested_at TEXT NOT NULL,
          last_run_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ingest_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          source TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          duration_ms REAL NOT NULL,
          items_seen INTEGER NOT NULL DEFAULT 0,
          items_new INTEGER NOT NULL DEFAULT 0,
          items_skipped INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_ingest_runs_run_id
          ON ingest_runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_ingest_runs_source_time
          ON ingest_runs(source, started_at);

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
          tokenize = 'unicode61 remove_diacritics 0'
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
          model TEXT,
          merge_with TEXT,
          dedup_similarity REAL,
          conflict_classification TEXT,
          conflict_confidence REAL,
          conflict_reason TEXT,
          conflict_model TEXT,
          conflict_checked_at TEXT,
          conflict_target_statement_hash TEXT,
          conflict_prompt_version INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_promo_status ON promotion_candidates(status);
        CREATE INDEX IF NOT EXISTS idx_promo_entity ON promotion_candidates(entity);
        """
    )
