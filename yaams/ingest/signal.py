"""Signal Desktop ingest adapter.

Reads the local Signal Desktop SQLCipher database, decrypts it via the
SQLCipher CLI using a key extracted from `config.json` (Chromium Safe
Storage scheme on macOS), then walks messages JOIN conversations and
yields Items. Attachments are emitted as separate Items linked back via
thread_id; reactions are folded into the parent message body.

The implementation deliberately shells out for the two privileged steps
(`security` for the Keychain wrapping password, `sqlcipher` for the
decrypt-to-plaintext export) to keep YAAMS aligned with the rest of the
codebase and avoid pulling SQLCipher native bindings into the Python env.
"""

from __future__ import annotations

import hashlib
import json as jsonlib
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc


CHROMIUM_SAFE_STORAGE_SALT = b"saltysalt"
CHROMIUM_SAFE_STORAGE_ITERATIONS = 1003
CHROMIUM_SAFE_STORAGE_KEY_LEN = 16
CHROMIUM_SAFE_STORAGE_IV = b" " * 16
CHROMIUM_SAFE_STORAGE_PREFIX = b"v10"

KEYCHAIN_SERVICE = "Signal Safe Storage"
KEYCHAIN_ACCOUNT = "Signal"

ALLOWED_MESSAGE_TYPES = {"incoming", "outgoing"}


@dataclass
class SignalAdapter:
  signal_dir: Path
  include_attachments: bool = True

  def extract(self, since: datetime) -> Iterator[Item]:
    signal_dir = expand_path(self.signal_dir)
    cutoff = ensure_utc(since)
    with tempfile.TemporaryDirectory(prefix="yaams-signal-") as tmpdir:
      tmp_path = Path(tmpdir)
      snapshot = snapshot_signal_db(signal_dir, tmp_path)
      sqlcipher_key = read_signal_key(signal_dir)
      plain_db = tmp_path / "signal-plain.db"
      export_plaintext_db(snapshot, sqlcipher_key, plain_db)
      conn = sqlite3.connect(f"file:{plain_db}?mode=ro", uri=True)
      conn.row_factory = sqlite3.Row
      try:
        yield from extract_from_connection(
          conn,
          cutoff,
          include_attachments=self.include_attachments,
        )
      finally:
        conn.close()


def snapshot_signal_db(signal_dir: Path, tmp: Path) -> Path:
  src_db = signal_dir / "sql" / "db.sqlite"
  if not src_db.exists():
    raise FileNotFoundError(src_db)
  for source in [
    src_db,
    src_db.with_name(src_db.name + "-wal"),
    src_db.with_name(src_db.name + "-shm"),
  ]:
    if source.exists():
      shutil.copy2(source, tmp / source.name)
  return tmp / src_db.name


def read_signal_key(signal_dir: Path) -> str:
  """Return the hex SQLCipher key for the Signal database."""
  config_path = signal_dir / "config.json"
  if not config_path.exists():
    raise FileNotFoundError(config_path)
  config = jsonlib.loads(config_path.read_text(encoding="utf-8"))
  legacy_key = config.get("key")
  if legacy_key:
    return str(legacy_key)
  encrypted_key = config.get("encryptedKey")
  if not encrypted_key:
    raise RuntimeError(
      "Signal config.json has neither 'key' nor 'encryptedKey'. Cannot decrypt."
    )
  backend = config.get("safeStorageBackend") or ""
  if backend and backend != "keychain_access":
    raise RuntimeError(
      f"Unsupported Signal safeStorageBackend: {backend!r}. "
      "Only 'keychain_access' is implemented for macOS."
    )
  password = fetch_keychain_password()
  return unwrap_encrypted_key(encrypted_key, password)


def fetch_keychain_password() -> bytes:
  result = subprocess.run(
    [
      "security",
      "find-generic-password",
      "-s",
      KEYCHAIN_SERVICE,
      "-a",
      KEYCHAIN_ACCOUNT,
      "-w",
    ],
    capture_output=True,
    text=False,
  )
  if result.returncode != 0:
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    raise RuntimeError(
      "Could not read Signal Safe Storage from Keychain: "
      f"{stderr or 'security exited non-zero'}"
    )
  return result.stdout.strip()


def unwrap_encrypted_key(encrypted_hex: str, password: bytes) -> str:
  ciphertext = bytes.fromhex(encrypted_hex)
  if ciphertext.startswith(CHROMIUM_SAFE_STORAGE_PREFIX):
    ciphertext = ciphertext[len(CHROMIUM_SAFE_STORAGE_PREFIX):]
  derived = derive_safe_storage_key(password)
  cipher = Cipher(algorithms.AES(derived), modes.CBC(CHROMIUM_SAFE_STORAGE_IV))
  decryptor = cipher.decryptor()
  padded = decryptor.update(ciphertext) + decryptor.finalize()
  plaintext = _strip_pkcs7(padded)
  return plaintext.decode("ascii")


def derive_safe_storage_key(password: bytes) -> bytes:
  return hashlib.pbkdf2_hmac(
    "sha1",
    password,
    CHROMIUM_SAFE_STORAGE_SALT,
    CHROMIUM_SAFE_STORAGE_ITERATIONS,
    dklen=CHROMIUM_SAFE_STORAGE_KEY_LEN,
  )


def _strip_pkcs7(data: bytes) -> bytes:
  if not data:
    return data
  pad = data[-1]
  if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
    return data
  return data[:-pad]


def export_plaintext_db(encrypted_db: Path, sqlcipher_key: str, target: Path) -> None:
  """Shell out to the sqlcipher CLI to export a plaintext copy."""
  if shutil.which("sqlcipher") is None:
    raise RuntimeError(
      "sqlcipher CLI is required for Signal ingest. Install with: brew install sqlcipher"
    )
  if target.exists():
    target.unlink()
  script = (
    f"PRAGMA key = \"x'{sqlcipher_key}'\";\n"
    f"ATTACH DATABASE '{target}' AS plaintext KEY '';\n"
    "SELECT sqlcipher_export('plaintext');\n"
    "DETACH DATABASE plaintext;\n"
  )
  result = subprocess.run(
    ["sqlcipher", str(encrypted_db)],
    input=script,
    capture_output=True,
    text=True,
  )
  if result.returncode != 0 or not target.exists():
    stderr = (result.stderr or "").strip()
    raise RuntimeError(f"sqlcipher decrypt failed: {stderr or 'no output'}")


def extract_from_connection(
  conn: sqlite3.Connection,
  since: datetime,
  *,
  include_attachments: bool = True,
) -> Iterator[Item]:
  message_columns = _table_columns(conn, "messages")
  if "id" not in message_columns or "conversationId" not in message_columns:
    raise RuntimeError("Signal messages table missing required columns: id, conversationId")
  has_received_at = "received_at" in message_columns
  since_ms = int(ensure_utc(since).timestamp() * 1000)

  conv_lookup = _build_conversation_lookup(conn)

  fields = [
    "m.id AS id",
    "m.conversationId AS conversationId",
    "m.type AS type",
    "m.sent_at AS sent_at",
    "m.body AS body",
    "m.json AS json",
  ]
  if "source" in message_columns:
    fields.append("m.source AS source")
  else:
    fields.append("NULL AS source")
  if has_received_at:
    fields.append("m.received_at AS received_at")
  else:
    fields.append("NULL AS received_at")

  query = f"""
    SELECT {', '.join(fields)}
    FROM messages m
    WHERE m.sent_at IS NOT NULL
      AND m.sent_at >= ?
      AND m.type IN ('incoming', 'outgoing')
    ORDER BY m.sent_at ASC
  """
  for row in conn.execute(query, (since_ms,)):
    payload = _safe_loads(row["json"]) or {}
    body = (row["body"] or payload.get("body") or "").strip()
    attachments = _normalize_attachments(payload.get("attachments") or [])
    reactions = _normalize_reactions(payload.get("reactions") or [])
    if not body and not attachments:
      continue

    is_outgoing = row["type"] == "outgoing"
    convo = conv_lookup.get(row["conversationId"], {})
    sender = _resolve_sender(is_outgoing, row, payload, convo)
    recipients = _resolve_recipients(is_outgoing, sender, convo)
    timestamp = _ms_to_datetime(int(row["sent_at"]))

    parent_metadata = {
      "is_outgoing": is_outgoing,
      "conversation_type": convo.get("type"),
      "group_name": convo.get("name") if convo.get("type") == "group" else None,
      "attachments": [
        {
          "file_name": a.get("file_name"),
          "content_type": a.get("content_type"),
          "size": a.get("size"),
          "attachment_id": f"{row['id']}:attachment:{idx}",
        }
        for idx, a in enumerate(attachments)
      ],
      "reactions": reactions,
      "quoted_message_id": (payload.get("quote") or {}).get("id"),
    }

    body_with_reactions = _fold_reactions(body, reactions)

    yield Item(
      id=hash_id("signal", row["id"]),
      source="signal",
      source_id=row["id"],
      timestamp=timestamp,
      sender=sender,
      recipients=recipients,
      content=body_with_reactions or _attachment_summary(attachments),
      subject=convo.get("name") if convo.get("type") == "group" else None,
      thread_id=row["conversationId"],
      raw_metadata=parent_metadata,
    )

    if include_attachments:
      for idx, attachment in enumerate(attachments):
        yield _attachment_item(
          parent_id=row["id"],
          parent_source_id=row["id"],
          conversation_id=row["conversationId"],
          sender=sender,
          recipients=recipients,
          subject_group=convo.get("name") if convo.get("type") == "group" else None,
          timestamp=timestamp,
          attachment=attachment,
          idx=idx,
        )


def _attachment_item(
  *,
  parent_id: str,
  parent_source_id: str,
  conversation_id: str,
  sender: str,
  recipients: list[str],
  subject_group: str | None,
  timestamp: datetime,
  attachment: dict,
  idx: int,
) -> Item:
  source_id = f"{parent_id}:attachment:{idx}"
  file_name = attachment.get("file_name") or "(unnamed)"
  content_type = attachment.get("content_type") or "application/octet-stream"
  size = attachment.get("size")
  size_str = f"{size} bytes" if isinstance(size, int) else "unknown size"
  content = f"Attachment: {file_name} ({content_type}, {size_str})"
  return Item(
    id=hash_id("signal", source_id),
    source="signal",
    source_id=source_id,
    timestamp=timestamp,
    sender=sender,
    recipients=recipients,
    content=content,
    subject=file_name if file_name != "(unnamed)" else subject_group,
    thread_id=conversation_id,
    raw_metadata={
      "parent_message_id": parent_id,
      "parent_source_id": parent_source_id,
      "attachment_index": idx,
      "file_name": attachment.get("file_name"),
      "content_type": attachment.get("content_type"),
      "size": attachment.get("size"),
      "attachment_path": attachment.get("path"),
    },
  )


def _attachment_summary(attachments: list[dict]) -> str:
  if not attachments:
    return ""
  names = [a.get("file_name") or "(unnamed)" for a in attachments]
  return "Attachments: " + ", ".join(names)


def _fold_reactions(body: str, reactions: list[dict]) -> str:
  if not reactions:
    return body
  parts = []
  for r in reactions:
    emoji = r.get("emoji") or ""
    who = r.get("from") or "(unknown)"
    parts.append(f"{emoji} from {who}".strip())
  footer = "Reactions: " + ", ".join(p for p in parts if p)
  return f"{body}\n\n{footer}" if body else footer


def _normalize_attachments(raw: list) -> list[dict]:
  out: list[dict] = []
  for entry in raw:
    if not isinstance(entry, dict):
      continue
    out.append(
      {
        "file_name": entry.get("fileName") or entry.get("file_name"),
        "content_type": entry.get("contentType") or entry.get("content_type"),
        "size": entry.get("size"),
        "path": entry.get("path"),
      }
    )
  return out


def _normalize_reactions(raw: list) -> list[dict]:
  out: list[dict] = []
  for entry in raw:
    if not isinstance(entry, dict):
      continue
    out.append(
      {
        "emoji": entry.get("emoji"),
        "from": entry.get("fromName") or entry.get("from") or entry.get("fromId"),
        "timestamp": entry.get("timestamp") or entry.get("targetTimestamp"),
      }
    )
  return out


def _resolve_sender(
  is_outgoing: bool,
  row: sqlite3.Row,
  payload: dict,
  convo: dict,
) -> str:
  if is_outgoing:
    return "me"
  if convo.get("type") == "private":
    return convo.get("name") or convo.get("id") or "(unknown)"
  for key in ("sourceServiceId", "sourceUuid", "source", "sourceE164"):
    value = payload.get(key)
    if value:
      label = convo.get("members_by_id", {}).get(value)
      if label:
        return label
      return str(value)
  source_field = row["source"] if "source" in row.keys() else None
  if source_field:
    return str(source_field)
  return "(unknown)"


def _resolve_recipients(
  is_outgoing: bool,
  sender: str,
  convo: dict,
) -> list[str]:
  if convo.get("type") == "group":
    members = list(convo.get("members_labels") or [])
    return [m for m in members if m != sender]
  if is_outgoing:
    if convo.get("type") == "private":
      label = convo.get("name") or convo.get("id")
      return [label] if label else []
    return []
  return ["me"]


def _build_conversation_lookup(conn: sqlite3.Connection) -> dict[str, dict]:
  cols = _table_columns(conn, "conversations")
  if not cols:
    return {}
  selects = ["id"]
  for c in ("type", "name", "profileFullName", "profileName", "e164", "serviceId", "members", "json"):
    if c in cols:
      selects.append(c)
  rows = conn.execute(f"SELECT {', '.join(selects)} FROM conversations").fetchall()
  by_id: dict[str, dict] = {}
  for row in rows:
    keys = row.keys()
    label = (
      _row_get(row, keys, "profileFullName")
      or _row_get(row, keys, "name")
      or _row_get(row, keys, "profileName")
      or _row_get(row, keys, "e164")
      or _row_get(row, keys, "serviceId")
      or row["id"]
    )
    members_raw = _safe_loads(_row_get(row, keys, "members") or "[]") or []
    by_id[row["id"]] = {
      "id": row["id"],
      "type": _row_get(row, keys, "type"),
      "name": label,
      "members_raw": members_raw,
    }

  for conv in by_id.values():
    member_ids = conv.get("members_raw") or []
    members_by_id: dict[str, str] = {}
    members_labels: list[str] = []
    for mid in member_ids:
      member = by_id.get(mid)
      if member:
        members_by_id[mid] = member["name"]
        members_labels.append(member["name"])
      else:
        members_by_id[mid] = mid
        members_labels.append(mid)
    conv["members_by_id"] = members_by_id
    conv["members_labels"] = members_labels
  return by_id


def _row_get(row: sqlite3.Row, keys, name: str):
  if name in keys:
    return row[name]
  return None


def _safe_loads(value):
  if not value:
    return None
  try:
    return jsonlib.loads(value)
  except (jsonlib.JSONDecodeError, TypeError):
    return None


def _ms_to_datetime(ms: int) -> datetime:
  return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
  names: set[str] = set()
  for row in conn.execute(f"PRAGMA table_info({table})"):
    names.add(row["name"] if isinstance(row, sqlite3.Row) else row[1])
  return names
