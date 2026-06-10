"""Add consolidated_into column and index to items table."""
from __future__ import annotations

import sqlite3

name = "0002_items_consolidated_into"
description = "Add items.consolidated_into column and idx_items_consolidated index"


def apply(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "consolidated_into" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN consolidated_into TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_consolidated ON items(consolidated_into)"
    )
