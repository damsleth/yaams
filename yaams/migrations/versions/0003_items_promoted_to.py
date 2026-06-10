"""Add promoted_to column to items table."""
from __future__ import annotations

import sqlite3

name = "0003_items_promoted_to"
description = "Add items.promoted_to column"


def apply(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "promoted_to" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN promoted_to TEXT")
