from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc


APPLE_EPOCH_UNIX_SECONDS = 978_307_200


@dataclass(frozen=True)
class IMessageAdapter:
  chat_db_path: Path

  def extract(self, since: datetime) -> Iterator[Item]:
    source_path = expand_path(self.chat_db_path)
    cutoff = ensure_utc(since)
    with tempfile.TemporaryDirectory(prefix="yaams-imessage-") as tmpdir:
      tmp_db = copy_chat_db(source_path, Path(tmpdir))
      conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
      conn.row_factory = sqlite3.Row
      try:
        yield from extract_from_connection(conn, cutoff)
      finally:
        conn.close()


def copy_chat_db(chat_db_path: Path, tmpdir: Path) -> Path:
  tmpdir.mkdir(parents=True, exist_ok=True)
  target = tmpdir / "chat.db"
  for source in [
    chat_db_path,
    chat_db_path.with_name(chat_db_path.name + "-wal"),
    chat_db_path.with_name(chat_db_path.name + "-shm"),
  ]:
    if source.exists():
      shutil.copy2(source, tmpdir / source.name)
  if not target.exists():
    raise FileNotFoundError(chat_db_path)
  return target


def extract_from_connection(
  conn: sqlite3.Connection,
  since: datetime,
) -> Iterator[Item]:
  columns = _table_columns(conn, "message")
  _require_columns(columns, {"guid", "text", "date", "is_from_me", "handle_id"})
  attributed = "m.attributedBody" if "attributedBody" in columns else "NULL"
  associated = (
    "m.associated_message_type"
    if "associated_message_type" in columns
    else "0"
  )
  attachments = (
    "m.cache_has_attachments"
    if "cache_has_attachments" in columns
    else "0"
  )
  reaction_filter = ""
  if "associated_message_type" in columns:
    reaction_filter = (
      "AND (m.associated_message_type IS NULL "
      "OR m.associated_message_type < 2000 "
      "OR m.associated_message_type > 3999)"
    )

  query = f"""
    SELECT
      m.ROWID AS rowid,
      m.guid AS guid,
      m.text AS text,
      {attributed} AS attributedBody,
      m.date AS date,
      m.is_from_me AS is_from_me,
      m.handle_id AS handle_id,
      {associated} AS associated_message_type,
      {attachments} AS cache_has_attachments,
      h.id AS handle_identifier,
      c.ROWID AS chat_id,
      c.guid AS chat_guid,
      c.display_name AS display_name
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    LEFT JOIN chat c ON cmj.chat_id = c.ROWID
    WHERE m.date >= ?
    {reaction_filter}
    ORDER BY m.date ASC
  """
  apple_since = datetime_to_apple_ts_for_db(conn, since)
  for row in conn.execute(query, (apple_since,)):
    text = extract_message_text(row["text"], row["attributedBody"]).strip()
    if not text:
      continue
    timestamp = apple_ts_to_datetime(row["date"])
    source_id = row["guid"] or f"rowid:{row['rowid']}"
    is_from_me = bool(row["is_from_me"])
    sender = "me" if is_from_me else (row["handle_identifier"] or "unknown")
    participants = fetch_chat_participants(conn, row["chat_id"])
    recipients = _recipients(sender, is_from_me, participants)
    yield Item(
      id=hash_id("imessage", source_id),
      source="imessage",
      source_id=source_id,
      timestamp=timestamp,
      sender=sender,
      recipients=recipients,
      content=text,
      thread_id=row["chat_guid"],
      raw_metadata={
        "chat_display_name": row["display_name"],
        "is_from_me": is_from_me,
        "has_attachments": bool(row["cache_has_attachments"]),
      },
    )


def apple_ts_to_datetime(apple_ts_nanos: int | float | None) -> datetime:
  if not apple_ts_nanos:
    return datetime(2001, 1, 1, tzinfo=UTC)
  value = float(apple_ts_nanos)
  seconds = value / 1e9 if abs(value) > 1e12 else value
  unix_seconds = seconds + APPLE_EPOCH_UNIX_SECONDS
  return datetime.fromtimestamp(unix_seconds, tz=UTC)


def datetime_to_apple_ts_nanos(value: datetime) -> int:
  utc_value = ensure_utc(value)
  return int((utc_value.timestamp() - APPLE_EPOCH_UNIX_SECONDS) * 1e9)


def datetime_to_apple_ts_seconds(value: datetime) -> int:
  utc_value = ensure_utc(value)
  return int(utc_value.timestamp() - APPLE_EPOCH_UNIX_SECONDS)


def datetime_to_apple_ts_for_db(
  conn: sqlite3.Connection,
  value: datetime,
) -> int:
  row = conn.execute("SELECT max(abs(date)) AS max_date FROM message").fetchone()
  max_date = row["max_date"] if isinstance(row, sqlite3.Row) else row[0]
  if max_date is not None and abs(float(max_date)) < 1e12:
    return datetime_to_apple_ts_seconds(value)
  return datetime_to_apple_ts_nanos(value)


def extract_message_text(
  text: str | None,
  attributed_body: bytes | None,
) -> str:
  if text:
    return text
  if not attributed_body:
    return ""
  try:
    import typedstream

    stream = typedstream.unarchive_from_data(attributed_body)
    for obj in getattr(stream, "contents", []):
      value = getattr(obj, "value", None)
      if isinstance(value, str):
        return value
  except Exception:
    pass
  decoded = attributed_body.decode("utf-8", errors="ignore")
  matches = re.findall(r"[\w\s.,!?@:#/+-]{3,}", decoded)
  return max(matches, key=len).strip() if matches else ""


def fetch_chat_participants(
  conn: sqlite3.Connection,
  chat_id: int | None,
) -> list[str]:
  if chat_id is None:
    return []
  rows = conn.execute(
    """
    SELECT DISTINCT h.id AS handle_identifier
    FROM chat_handle_join chj
    JOIN handle h ON h.ROWID = chj.handle_id
    WHERE chj.chat_id = ?
    ORDER BY h.id
    """,
    (chat_id,),
  ).fetchall()
  return [row["handle_identifier"] for row in rows if row["handle_identifier"]]


def _recipients(
  sender: str,
  is_from_me: bool,
  participants: list[str],
) -> list[str]:
  if is_from_me:
    return [p for p in participants if p != "me"]
  return ["me"] + [p for p in participants if p not in {sender, "me"}]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
  names: set[str] = set()
  for row in conn.execute(f"PRAGMA table_info({table})"):
    names.add(row["name"] if isinstance(row, sqlite3.Row) else row[1])
  return names


def _require_columns(actual: set[str], required: set[str]) -> None:
  missing = sorted(required - actual)
  if missing:
    joined = ", ".join(missing)
    raise RuntimeError(f"chat.db message table missing required columns: {joined}")
