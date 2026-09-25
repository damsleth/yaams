"""Item-side usage derived from the append-only query log. Read-only.

Surfaced counts carry exposure bias (the ranker picks what gets surfaced), so
they are for offline review lists only, never live scoring; only `cited`
carries human-validated demand.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from yaams.signals.logger import VERDICT_ROW

# Stricter than the review queue's ('legacy', 'eval'): review keeps 'test' so
# fixture rows can be judged, but fixture traffic is never real usage.
EXCLUDE_PROVENANCE = ("eval", "test", "legacy")


@dataclass
class ItemUsage:
  item_id: str
  surfaced_count: int
  cited_count: int
  last_surfaced_at: str


def item_usage(
  conn: sqlite3.Connection,
  *,
  exclude_provenance: tuple[str, ...] = EXCLUDE_PROVENANCE,
) -> dict[str, ItemUsage]:
  """Per-result usage over real traffic: drops excluded provenances and queries
  whose latest feedback verdict is `noise`."""
  placeholders = ",".join("?" * len(exclude_provenance)) or "''"
  rows = conn.execute(
    f"""
    SELECT r.result_id, COUNT(*), SUM(r.cited), MAX(q.ts)
    FROM query_results r JOIN queries q ON q.id = r.query_id
    WHERE COALESCE(q.provenance, '') NOT IN ({placeholders})
      AND COALESCE((
        SELECT f.kind FROM query_feedback f
        WHERE f.query_id = q.id AND f.{VERDICT_ROW} AND f.kind != 'deferred'
        ORDER BY f.id DESC LIMIT 1
      ), '') != 'noise'
    GROUP BY r.result_id
    """,
    exclude_provenance,
  ).fetchall()
  return {r[0]: ItemUsage(r[0], int(r[1]), int(r[2] or 0), r[3]) for r in rows}


def stale_tier2(
  conn: sqlite3.Connection, usage: dict[str, ItemUsage], *, since_ts: str, tier2_source: str = "tier2_ledger"
) -> list[tuple[str, str]]:
  """tier2 items never surfaced since `since_ts`: (item_id, source_id), an archive-review list."""
  rows = conn.execute(
    "SELECT id, source_id FROM items WHERE source = ? ORDER BY source_id", (tier2_source,)
  ).fetchall()
  return [
    (r[0], r[1]) for r in rows
    if r[0] not in usage or usage[r[0]].last_surfaced_at < since_ts
  ]
