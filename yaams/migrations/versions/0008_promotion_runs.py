"""Run-level provenance for promotion: promotion_runs + candidate run/state fields (plan PR 3)."""
from __future__ import annotations

import sqlite3

name = "0008_promotion_runs"
description = (
  "Create promotion_runs table; add run_id, candidate state machine and "
  "contract-v1 export fields to promotion_candidates"
)

_CANDIDATE_COLUMNS = [
  ("run_id", "TEXT"),
  ("candidate_schema_version", "INTEGER"),
  ("proposed_action", "TEXT"),
  ("target_path", "TEXT"),
  ("target_statement_hash", "TEXT"),
  ("evidence_map", "TEXT"),
  ("generator_confidence", "REAL"),
  ("mechanical", "TEXT"),
  ("judge", "TEXT"),
  ("stage", "TEXT"),
  ("stage_reason", "TEXT"),
  ("gate_status", "TEXT"),
  ("human_disposition", "TEXT"),
]


def apply(conn: sqlite3.Connection) -> None:
  conn.execute(
    """
    CREATE TABLE IF NOT EXISTS promotion_runs (
      run_id TEXT PRIMARY KEY,
      created_at TEXT NOT NULL,
      snapshot_id TEXT,
      selection_start TEXT,
      selection_end TEXT,
      backend TEXT,
      model TEXT,
      prompt_version TEXT,
      prompt_hash TEXT,
      temperature REAL,
      seed INTEGER,
      config_hash TEXT,
      item_set_hash TEXT,
      status TEXT NOT NULL DEFAULT 'running',
      duration_ms REAL,
      candidate_count INTEGER,
      tokens INTEGER,
      cost REAL,
      parent_run_id TEXT
    )
    """
  )
  existing = {row[1] for row in conn.execute("PRAGMA table_info(promotion_candidates)")}
  for col, coltype in _CANDIDATE_COLUMNS:
    if col not in existing:
      conn.execute(f"ALTER TABLE promotion_candidates ADD COLUMN {col} {coltype}")
  conn.execute("CREATE INDEX IF NOT EXISTS idx_promo_run ON promotion_candidates(run_id)")
