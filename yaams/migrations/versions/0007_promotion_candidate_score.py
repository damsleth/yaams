"""Add multi-factor admission score columns to promotion_candidates."""
from __future__ import annotations

import sqlite3

name = "0007_promotion_candidate_score"
description = (
    "Add admission_score (REAL) and admission_factors (TEXT/JSON) to "
    "promotion_candidates; populated by `promote generate` so review can rank "
    "best-first and `commit --min-score` can gate. NULL on legacy rows."
)


def apply(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(promotion_candidates)")}
    if "admission_score" not in cols:
        conn.execute("ALTER TABLE promotion_candidates ADD COLUMN admission_score REAL")
    if "admission_factors" not in cols:
        conn.execute("ALTER TABLE promotion_candidates ADD COLUMN admission_factors TEXT")
