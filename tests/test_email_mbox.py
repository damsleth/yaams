from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

from yaams.ingest.email_mbox import email_to_item, parse_emlx


def make_message() -> EmailMessage:
  message = EmailMessage()
  message["Date"] = "Wed, 29 Apr 2026 12:00:00 +0200"
  message["From"] = "Alice <alice@example.test>"
  message["To"] = "Bob <bob@example.test>"
  message["Cc"] = "Alice <alice@example.test>"
  message["Subject"] = "Cabin"
  message["Message-ID"] = "<msg-1@example.test>"
  message.set_content("Hei Alice, cabin plan is still on.")
  return message


def test_email_to_item_extracts_plain_text_and_headers():
  item = email_to_item(make_message(), datetime(2026, 1, 1, tzinfo=UTC))

  assert item is not None
  assert item.source == "email"
  assert item.source_id == "<msg-1@example.test>"
  assert item.sender == "alice@example.test"
  assert item.recipients == ["bob@example.test", "alice@example.test"]
  assert item.timestamp == datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
  assert "cabin plan" in item.content


def test_parse_emlx_uses_length_prefix(tmp_path):
  raw_message = make_message().as_bytes()
  emlx = tmp_path / "message.emlx"
  emlx.write_bytes(str(len(raw_message)).encode() + b"\n" + raw_message + b"\n{}")

  parsed = parse_emlx(emlx)

  assert parsed["Message-ID"] == "<msg-1@example.test>"
  assert parsed.get_content().strip() == "Hei Alice, cabin plan is still on."

