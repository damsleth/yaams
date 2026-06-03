from __future__ import annotations

from datetime import UTC, datetime

from yaams.ingest.m365_mail import (
  M365MailAdapter,
  _is_owa_newsletter,
  _split_addresses,
  _to_item,
)


def _message(**overrides) -> dict:
  """A message shaped like `owa-mail messages --with-body` output."""
  m = {
    "id": "msg-1",
    "conversation_id": "thr-1",
    "received": "2026-05-23T13:20:46Z",
    "subject": "Hello",
    "from": "alice@example.com",
    "to": "bob@example.com",
    "cc": "",
    "bcc": "",
    "body_type": "Text",
    "body": "Just checking in.",
    "has_attachments": False,
    "is_read": False,
    "importance": "Normal",
    "web_link": "https://outlook.office.com/...",
    "internet_headers": [],
  }
  m.update(overrides)
  return m


def test_to_item_basic_text_body():
  item = _to_item(_message(), folder="Inbox", profile="work")
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
    _message(body=html, body_type="HTML"),
    folder="Inbox", profile="work",
  )
  assert item is not None
  assert "<" not in item.content
  assert "Hi" in item.content
  assert "there" in item.content


def test_to_item_returns_none_for_empty_body():
  item = _to_item(_message(body=""), folder="Inbox", profile="work")
  assert item is None


def test_to_item_returns_none_for_missing_timestamp():
  item = _to_item(_message(received=""), folder="Inbox", profile="work")
  assert item is None


def test_split_addresses_handles_csv_and_blanks():
  assert _split_addresses("a@x.com, b@y.com") == ["a@x.com", "b@y.com"]
  assert _split_addresses("") == []
  assert _split_addresses("  ,  a@x.com  ,  ") == ["a@x.com"]


def test_is_owa_newsletter_detects_subject_keywords():
  assert _is_owa_newsletter(_message(subject="Weekly update from foo"))
  assert _is_owa_newsletter(_message(subject="Click here to unsubscribe"))
  assert not _is_owa_newsletter(_message(subject="Lunch tomorrow?"))


def test_is_owa_newsletter_detects_norwegian_subjects():
  assert _is_owa_newsletter(_message(subject="Nyhetsbrev uke 23 – Røde Kors"))
  assert _is_owa_newsletter(_message(subject="IKKE SVAR: kvittering"))
  assert _is_owa_newsletter(_message(subject="Denne e-posten skal ikke besvares"))
  assert _is_owa_newsletter(_message(subject="Klikk her for å avmelde deg"))
  assert not _is_owa_newsletter(_message(subject="Lunsj i morgen?"))
  assert not _is_owa_newsletter(_message(subject="Referat fra lokalrådsmøtet"))


def test_is_owa_newsletter_detects_list_unsubscribe_header():
  msg = _message(
    subject="Quarterly report",
    internet_headers=[
      {"name": "List-Unsubscribe", "value": "<mailto:unsub@example.com>"},
    ],
  )
  assert _is_owa_newsletter(msg)


def test_is_owa_newsletter_detects_precedence_bulk():
  msg = _message(
    subject="Quarterly report",
    internet_headers=[{"name": "Precedence", "value": "bulk"}],
  )
  assert _is_owa_newsletter(msg)


def test_is_owa_newsletter_ignores_auto_submitted_no():
  msg = _message(
    subject="Quarterly report",
    internet_headers=[{"name": "Auto-Submitted", "value": "no"}],
  )
  assert not _is_owa_newsletter(msg)


def test_is_owa_newsletter_detects_auto_submitted_generated():
  msg = _message(
    subject="Quarterly report",
    internet_headers=[{"name": "Auto-Submitted", "value": "auto-generated"}],
  )
  assert _is_owa_newsletter(msg)


def test_materialize_counts_no_timestamp_as_skip():
  adapter = M365MailAdapter(profile="work")
  adapter.skipped_no_timestamp = 0
  adapter.skipped_empty = 0
  msg = _message(received="")
  out = adapter._materialize(msg, folder="Inbox", user_set=set())
  assert out is None
  assert adapter.skipped_no_timestamp == 1
  assert adapter.skipped_empty == 0


def test_materialize_counts_empty_body_as_skip():
  adapter = M365MailAdapter(profile="work")
  adapter.skipped_no_timestamp = 0
  adapter.skipped_empty = 0
  msg = _message(body="")
  out = adapter._materialize(msg, folder="Inbox", user_set=set())
  assert out is None
  assert adapter.skipped_no_timestamp == 0
  assert adapter.skipped_empty == 1
