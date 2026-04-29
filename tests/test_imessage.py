from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from yaams.ingest.imessage import (
  IMessageAdapter,
  apple_ts_to_datetime,
  datetime_to_apple_ts_nanos,
  extract_message_text,
)


def test_apple_timestamp_round_trip():
  timestamp = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

  assert apple_ts_to_datetime(datetime_to_apple_ts_nanos(timestamp)) == timestamp


def test_attributed_body_parse_failure_returns_empty_text():
  body = b"NSMutableAttributedString probably-not-the-message-body"

  assert extract_message_text(None, body) == ""


def test_imessage_adapter_extracts_messages(tmp_path):
  chat_db = tmp_path / "chat.db"
  conn = sqlite3.connect(chat_db)
  conn.executescript(
    """
    CREATE TABLE message (
      guid TEXT,
      text TEXT,
      attributedBody BLOB,
      date INTEGER,
      is_from_me INTEGER,
      handle_id INTEGER,
      associated_message_type INTEGER,
      cache_has_attachments INTEGER
    );
    CREATE TABLE handle (
      id TEXT,
      service TEXT
    );
    CREATE TABLE chat (
      guid TEXT,
      chat_identifier TEXT,
      display_name TEXT
    );
    CREATE TABLE chat_message_join (
      chat_id INTEGER,
      message_id INTEGER
    );
    CREATE TABLE chat_handle_join (
      chat_id INTEGER,
      handle_id INTEGER
    );
    """
  )
  sent_at = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
  conn.execute(
    "INSERT INTO handle (ROWID, id, service) VALUES (1, ?, 'iMessage')",
    ("+4712345678",),
  )
  conn.execute(
    "INSERT INTO chat (ROWID, guid, chat_identifier, display_name) VALUES (1, ?, ?, ?)",
    ("chat-guid", "+4712345678", None),
  )
  conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
  conn.execute(
    """
    INSERT INTO message (
      ROWID, guid, text, attributedBody, date, is_from_me, handle_id,
      associated_message_type, cache_has_attachments
    )
    VALUES (1, ?, ?, NULL, ?, 1, 1, 0, 0)
    """,
    ("message-guid", "Hei Alice", datetime_to_apple_ts_nanos(sent_at)),
  )
  conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
  conn.commit()
  conn.close()

  items = list(IMessageAdapter(chat_db).extract(datetime(2026, 1, 1, tzinfo=UTC)))

  assert len(items) == 1
  assert items[0].source == "imessage"
  assert items[0].source_id == "message-guid"
  assert items[0].sender == "me"
  assert items[0].recipients == ["+4712345678"]
  assert items[0].content == "Hei Alice"
  assert items[0].thread_id == "chat-guid"


def test_imessage_adapter_skips_zero_dates_and_labels_unknown_senders(tmp_path):
  chat_db = tmp_path / "chat.db"
  conn = sqlite3.connect(chat_db)
  conn.executescript(
    """
    CREATE TABLE message (
      guid TEXT,
      text TEXT,
      attributedBody BLOB,
      date INTEGER,
      is_from_me INTEGER,
      handle_id INTEGER,
      associated_message_type INTEGER,
      cache_has_attachments INTEGER
    );
    CREATE TABLE handle (
      id TEXT,
      service TEXT
    );
    CREATE TABLE chat (
      guid TEXT,
      chat_identifier TEXT,
      display_name TEXT
    );
    CREATE TABLE chat_message_join (
      chat_id INTEGER,
      message_id INTEGER
    );
    CREATE TABLE chat_handle_join (
      chat_id INTEGER,
      handle_id INTEGER
    );
    """
  )
  sent_at = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
  conn.execute(
    "INSERT INTO chat (ROWID, guid, chat_identifier, display_name) VALUES (1, ?, ?, ?)",
    ("chat-guid", "unknown", None),
  )
  conn.execute(
    """
    INSERT INTO message (
      ROWID, guid, text, attributedBody, date, is_from_me, handle_id,
      associated_message_type, cache_has_attachments
    )
    VALUES (1, ?, ?, NULL, 0, 0, NULL, 0, 0)
    """,
    ("zero-date", "skip me"),
  )
  conn.execute(
    """
    INSERT INTO message (
      ROWID, guid, text, attributedBody, date, is_from_me, handle_id,
      associated_message_type, cache_has_attachments
    )
    VALUES (2, ?, ?, NULL, ?, 0, 42, 0, 0)
    """,
    ("unknown-sender", "keep me", datetime_to_apple_ts_nanos(sent_at)),
  )
  conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
  conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 2)")
  conn.commit()
  conn.close()

  items = list(IMessageAdapter(chat_db).extract(datetime(2000, 1, 1, tzinfo=UTC)))

  assert [item.source_id for item in items] == ["unknown-sender"]
  assert items[0].sender == "unknown:handle:42"
