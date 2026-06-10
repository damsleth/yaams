from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from yaams.ingest.signal import (
  CHROMIUM_SAFE_STORAGE_IV,
  CHROMIUM_SAFE_STORAGE_PREFIX,
  _validate_hex_key,
  derive_safe_storage_key,
  extract_from_connection,
  unwrap_encrypted_key,
)


def test_validate_hex_key_accepts_hex():
  key = "ab" * 32
  assert _validate_hex_key(key) == key


@pytest.mark.parametrize("bad", ["", "nothex", "ab'; DROP", "ab cd", None])
def test_validate_hex_key_rejects_non_hex(bad):
  with pytest.raises(RuntimeError, match="not valid hex"):
    _validate_hex_key(bad)


def _signal_schema(conn: sqlite3.Connection) -> None:
  conn.executescript(
    """
    CREATE TABLE messages (
      id TEXT PRIMARY KEY,
      conversationId TEXT NOT NULL,
      type TEXT,
      sent_at INTEGER,
      received_at INTEGER,
      source TEXT,
      sourceServiceId TEXT,
      body TEXT,
      json TEXT
    );
    CREATE TABLE conversations (
      id TEXT PRIMARY KEY,
      type TEXT,
      name TEXT,
      profileFullName TEXT,
      profileName TEXT,
      e164 TEXT,
      serviceId TEXT,
      members TEXT,
      json TEXT
    );
    """
  )


def _open_db():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  _signal_schema(conn)
  return conn


def _insert_conversation(conn, **kwargs):
  cols = list(kwargs.keys())
  placeholders = ",".join("?" * len(cols))
  conn.execute(
    f"INSERT INTO conversations ({','.join(cols)}) VALUES ({placeholders})",
    [kwargs[c] for c in cols],
  )


def _insert_message(conn, **kwargs):
  cols = list(kwargs.keys())
  placeholders = ",".join("?" * len(cols))
  conn.execute(
    f"INSERT INTO messages ({','.join(cols)}) VALUES ({placeholders})",
    [kwargs[c] for c in cols],
  )


def _ms(year, month, day, hour=12, minute=0):
  return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def test_derive_key_matches_chromium_constants():
  derived = derive_safe_storage_key(b"hunter2")
  assert len(derived) == 16
  derived_again = derive_safe_storage_key(b"hunter2")
  assert derived == derived_again
  assert derived != derive_safe_storage_key(b"different")


def test_unwrap_encrypted_key_round_trip():
  password = b"signal-keychain-password"
  derived = derive_safe_storage_key(password)
  sqlcipher_hex = "a" * 64

  cipher = Cipher(algorithms.AES(derived), modes.CBC(CHROMIUM_SAFE_STORAGE_IV))
  encryptor = cipher.encryptor()
  data = sqlcipher_hex.encode("ascii")
  pad_len = 16 - (len(data) % 16)
  padded = data + bytes([pad_len] * pad_len)
  ciphertext = CHROMIUM_SAFE_STORAGE_PREFIX + encryptor.update(padded) + encryptor.finalize()

  recovered = unwrap_encrypted_key(ciphertext.hex(), password)
  assert recovered == sqlcipher_hex


def test_extract_skips_control_messages_and_empty_bodies():
  conn = _open_db()
  _insert_conversation(conn, id="c1", type="private", profileFullName="Alice", e164="+47000")
  _insert_message(
    conn, id="m1", conversationId="c1", type="incoming",
    sent_at=_ms(2026, 4, 1), source="+47000", body="hello there", json="{}",
  )
  _insert_message(
    conn, id="m2", conversationId="c1", type="verified-change",
    sent_at=_ms(2026, 4, 2), body="(verified change)", json="{}",
  )
  _insert_message(
    conn, id="m3", conversationId="c1", type="incoming",
    sent_at=_ms(2026, 4, 3), source="+47000", body="", json="{}",
  )

  items = list(extract_from_connection(conn, datetime(2026, 1, 1, tzinfo=UTC)))
  assert [i.source_id for i in items] == ["m1"]
  assert items[0].sender == "Alice"
  assert items[0].thread_id == "c1"
  assert items[0].recipients == ["me"]


def test_extract_outgoing_message_is_from_me():
  conn = _open_db()
  _insert_conversation(conn, id="c1", type="private", profileFullName="Bob")
  _insert_message(
    conn, id="m-out", conversationId="c1", type="outgoing",
    sent_at=_ms(2026, 4, 5), body="see you tomorrow", json="{}",
  )

  items = list(extract_from_connection(conn, datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  assert items[0].sender == "me"
  assert items[0].recipients == ["Bob"]


def test_extract_group_message_resolves_members_and_subject():
  conn = _open_db()
  _insert_conversation(
    conn, id="alice", type="private", profileFullName="Alice",
    serviceId="aci-alice",
  )
  _insert_conversation(
    conn, id="bob", type="private", profileFullName="Bob",
    serviceId="aci-bob",
  )
  _insert_conversation(
    conn, id="me", type="private", profileFullName="Me",
    serviceId="aci-me",
  )
  _insert_conversation(
    conn, id="g1", type="group", name="Family",
    json=json.dumps(
      {
        "membersV2": [
          {"aci": "aci-alice", "role": 2},
          {"aci": "aci-bob", "role": 2},
          {"aci": "aci-me", "role": 2},
        ]
      }
    ),
  )
  _insert_message(
    conn, id="g-m1", conversationId="g1", type="incoming",
    sent_at=_ms(2026, 4, 6),
    body="dinner saturday?",
    sourceServiceId="aci-alice",
    json="{}",
  )

  items = list(extract_from_connection(conn, datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  item = items[0]
  assert item.thread_id == "g1"
  assert item.subject == "Family"
  assert item.sender == "Alice"
  assert "Bob" in item.recipients
  assert "Me" in item.recipients
  assert "Alice" not in item.recipients
  assert item.raw_metadata["conversation_type"] == "group"
  assert item.raw_metadata["group_name"] == "Family"


def test_extract_attachments_emit_separate_items_linked_to_parent():
  conn = _open_db()
  _insert_conversation(conn, id="c1", type="private", profileFullName="Alice")
  _insert_message(
    conn, id="m-attach", conversationId="c1", type="incoming",
    sent_at=_ms(2026, 4, 7), source="alice", body="check this out",
    json=json.dumps(
      {
        "attachments": [
          {"fileName": "cabin.jpg", "contentType": "image/jpeg", "size": 12345, "path": "ab/cd"},
          {"fileName": "notes.pdf", "contentType": "application/pdf", "size": 6789, "path": "ef/gh"},
        ]
      }
    ),
  )

  items = list(extract_from_connection(conn, datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 3
  parent, a1, a2 = items
  assert parent.source_id == "m-attach"
  assert parent.raw_metadata["attachments"][0]["file_name"] == "cabin.jpg"
  assert a1.source_id == "m-attach:attachment:0"
  assert a1.raw_metadata["parent_message_id"] == "m-attach"
  assert a1.raw_metadata["file_name"] == "cabin.jpg"
  assert a1.raw_metadata["content_type"] == "image/jpeg"
  assert "cabin.jpg" in a1.content
  assert "image/jpeg" in a1.content
  assert a2.source_id == "m-attach:attachment:1"
  assert a2.thread_id == "c1"


def test_extract_skip_attachments_when_disabled():
  conn = _open_db()
  _insert_conversation(conn, id="c1", type="private", profileFullName="Alice")
  _insert_message(
    conn, id="m1", conversationId="c1", type="incoming",
    sent_at=_ms(2026, 4, 7), source="alice", body="see attached",
    json=json.dumps({"attachments": [{"fileName": "x.png", "contentType": "image/png", "size": 1}]}),
  )
  items = list(
    extract_from_connection(conn, datetime(2026, 1, 1, tzinfo=UTC), include_attachments=False)
  )
  assert len(items) == 1
  assert items[0].source_id == "m1"


def test_extract_folds_reactions_into_parent_body():
  conn = _open_db()
  _insert_conversation(conn, id="c1", type="private", profileFullName="Alice")
  _insert_message(
    conn, id="m-react", conversationId="c1", type="outgoing",
    sent_at=_ms(2026, 4, 8), body="ship it",
    json=json.dumps(
      {
        "reactions": [
          {"emoji": "👍", "fromName": "Alice", "timestamp": 1700000000000},
          {"emoji": "🚀", "fromName": "Bob", "timestamp": 1700000001000},
        ]
      }
    ),
  )

  items = list(extract_from_connection(conn, datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  parent = items[0]
  assert "ship it" in parent.content
  assert "reactions:" in parent.content
  assert "👍" in parent.content and "🚀" in parent.content
  assert parent.raw_metadata["reactions"][0]["emoji"] == "👍"
  assert parent.raw_metadata["reactions"][0]["from"] == "Alice"


def test_extract_respects_since_cutoff():
  conn = _open_db()
  _insert_conversation(conn, id="c1", type="private", profileFullName="Alice")
  _insert_message(
    conn, id="old", conversationId="c1", type="incoming",
    sent_at=_ms(2025, 1, 1), source="alice", body="ancient", json="{}",
  )
  _insert_message(
    conn, id="new", conversationId="c1", type="incoming",
    sent_at=_ms(2026, 1, 1), source="alice", body="recent", json="{}",
  )
  items = list(extract_from_connection(conn, datetime(2025, 6, 1, tzinfo=UTC)))
  assert [i.source_id for i in items] == ["new"]
