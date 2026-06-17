"""Add an origin-trust provenance column to the items table."""
from __future__ import annotations

import sqlite3

name = "0006_items_provenance"
description = (
    "Add nullable provenance column to items (origin-trust class derived from "
    "the ingest source); NULL rows derive provenance at query time"
)


def apply(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    # provenance: origin-trust class for the item (curated, authored,
    # structured, conversational, imported, inferred). Populated at ingest;
    # NULL for rows written before this column existed -- derived from the
    # source at query time (see yaams.trust.derive_provenance).
    if "provenance" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN provenance TEXT")
