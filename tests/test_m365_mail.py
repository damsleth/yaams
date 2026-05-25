from __future__ import annotations

from datetime import UTC, datetime

from yaams.ingest.m365_mail import _is_owa_newsletter, _split_addresses, _to_item


def _envelope(**overrides) -> dict:
  env = {
    "id": "msg-1",
    "conversation_id": "thr-1",
    "received": "2026-05-23T13:20:46Z",
    "subject": "Hello",
    "from": "alice@example.com",
    "to": "bob@example.com",
    "cc": "",
    "bcc": "",
  }
  env.update(overrides)
  return env


def _body(**overrides) -> dict:
  body = dict(_envelope())
  body.update({
    "body_type": "Text",
    "body": "Just checking in.",
    "has_attachments": False,
    "is_read": False,
    "importance": "Normal",
    "web_link": "https://outlook.office.com/...",
  })
  body.update(overrides)
  return body


def test_to_item_basic_text_body():
  item = _to_item(_body(), _envelope(), folder="Inbox", profile="work")
  assert item is not None
  assert item.source == "mail_work"
  assert item.source_id == "msg-1"
  assert item.timestamp == datetime(2026, 5, 23, 13, 20, 46, tzinfo=UTC)
  assert item.sender == "alice@example.com"
  assert item.recipients == ["bob@example.com"]
  assert item.thread_id == "thr-1"
  assert item.subject == "Hello"
  assert "Just checking in" in item.content
  assert item.raw_metadata["folder"] == "Inbox"


def test_to_item_strips_html_body():
  html = "<html><body><p>Hi <b>there</b></p></body></html>"
  item = _to_item(
    _body(body=html, body_type="HTML"), _envelope(),
    folder="Inbox", profile="work",
  )
  assert item is not None
  assert "<" not in item.content
  assert "Hi" in item.content
  assert "there" in item.content


def test_to_item_returns_none_for_empty_body():
  item = _to_item(
    _body(body=""), _envelope(), folder="Inbox", profile="work",
  )
  assert item is None


def test_to_item_returns_none_for_missing_timestamp():
  item = _to_item(
    _body(received=""), _envelope(received=""),
    folder="Inbox", profile="work",
  )
  assert item is None


def test_split_addresses_handles_csv_and_blanks():
  assert _split_addresses("a@x.com, b@y.com") == ["a@x.com", "b@y.com"]
  assert _split_addresses("") == []
  assert _split_addresses("  ,  a@x.com  ,  ") == ["a@x.com"]


def test_is_owa_newsletter_detects_subject_keywords():
  assert _is_owa_newsletter(_envelope(subject="Weekly update from foo"))
  assert _is_owa_newsletter(_envelope(subject="Click here to unsubscribe"))
  assert not _is_owa_newsletter(_envelope(subject="Lunch tomorrow?"))
