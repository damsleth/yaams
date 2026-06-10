"""Add structured query fields to the queries table."""
from __future__ import annotations

import sqlite3

name = "0005_query_structured_fields"
description = (
    "Add parsed_query, shape, confidence, confidence_reason, gaps, "
    "parser_fallback, and provenance columns to queries table"
)


def apply(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(queries)")}
    additions = (
        ("parsed_query", "TEXT"),
        ("shape", "TEXT"),
        ("confidence", "TEXT"),
        ("confidence_reason", "TEXT"),
        ("gaps", "TEXT"),
        ("parser_fallback", "INTEGER NOT NULL DEFAULT 0"),
        # provenance: who issued the query (cli, test, unknown, ...).
        # NULL for rows logged before this column existed -- treat as 'unknown'.
        ("provenance", "TEXT"),
    )
    for col, decl in additions:
        if col not in cols:
            conn.execute(f"ALTER TABLE queries ADD COLUMN {col} {decl}")
