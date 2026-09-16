"""Retrieval-quality annotation on raw items: junk_reason (+ lookup indexes)."""
from __future__ import annotations

import sqlite3

name = "0009_junk_reason"
description = (
  "Add items.junk_reason (NULL = retrievable; 'mech:*' / 'llm:*' = excluded from "
  "retrieval when retrieve.exclude_junk is on), an index on it, and a "
  "(thread_id, timestamp) index for thread-context lookups"
)

# Raw items stay immutable in content; this is an annotation column in the
# same family as consolidated_into / promoted_to. Reasons are prefixed so
# exclusion and reversal can be per-category:
#   mech:short     messaging row under 10 chars after trim
#   mech:reaction  iMessage tapback text ("Liked ...", "Emphasized ...")
#   mech:dup       same content + thread + sender + calendar day; earliest kept
#   llm:junk       Sonnet judged it non-retrievable, on two-run agreement
# Reverse any category with: UPDATE items SET junk_reason = NULL WHERE junk_reason LIKE 'mech:dup%'


def apply(conn: sqlite3.Connection) -> None:
  existing = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
  if "junk_reason" not in existing:
    conn.execute("ALTER TABLE items ADD COLUMN junk_reason TEXT")
  conn.execute("CREATE INDEX IF NOT EXISTS idx_items_junk_reason ON items(junk_reason)")
  conn.execute("CREATE INDEX IF NOT EXISTS idx_items_thread_ts ON items(thread_id, timestamp)")
