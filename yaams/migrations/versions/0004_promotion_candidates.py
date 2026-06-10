"""Create promotion_candidates table with dedup and conflict-detection fields."""
from __future__ import annotations

import sqlite3

name = "0004_promotion_candidates"
description = (
    "Create promotion_candidates table; add dedup fields (plan 38) "
    "and conflict-detection fields (plan 40)"
)


def apply(conn: sqlite3.Connection) -> None:
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

    # Phase C (plan 38): dedup fields
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(promotion_candidates)")}
    for col, coltype in [
        ("merge_with", "TEXT"),
        ("dedup_similarity", "REAL"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE promotion_candidates ADD COLUMN {col} {coltype}")

    # Phase E (plan 40): conflict detection fields
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(promotion_candidates)")}
    for col, coltype in [
        ("conflict_classification", "TEXT"),
        ("conflict_confidence", "REAL"),
        ("conflict_reason", "TEXT"),
        ("conflict_model", "TEXT"),
        ("conflict_checked_at", "TEXT"),
        ("conflict_target_statement_hash", "TEXT"),
        ("conflict_prompt_version", "INTEGER"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE promotion_candidates ADD COLUMN {col} {coltype}")
