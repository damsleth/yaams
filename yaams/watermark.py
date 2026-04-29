from __future__ import annotations

import sqlite3
from datetime import datetime

from yaams.time import ensure_utc, parse_iso_datetime, utc_now


def get_watermark(conn: sqlite3.Connection, source: str) -> datetime | None:
  row = conn.execute(
    "SELECT last_ingested_at FROM watermarks WHERE source = ?",
    (source,),
  ).fetchone()
  if row is None:
    return None
  return parse_iso_datetime(row["last_ingested_at"])


def update_watermark(
  conn: sqlite3.Connection,
  source: str,
  last_ingested_at: datetime,
) -> None:
  latest = ensure_utc(last_ingested_at)
  now = utc_now()
  conn.execute(
    """
    INSERT INTO watermarks (source, last_ingested_at, last_run_at)
    VALUES (?, ?, ?)
    ON CONFLICT(source) DO UPDATE SET
      last_ingested_at = excluded.last_ingested_at,
      last_run_at = excluded.last_run_at
    """,
    (source, latest.isoformat(), now.isoformat()),
  )

