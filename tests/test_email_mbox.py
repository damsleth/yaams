from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

from yaams.ingest.email_mbox import (
  EmailAdapter,
  clean_email_body,
  email_to_item,
  parse_email_datetime,
  parse_emlx,
  strip_html,
)


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


def test_email_to_item_accepts_date_only_header():
  message = make_message()
  message.replace_header("Date", "Thursday, April 10, 2014")

  item = email_to_item(message, datetime(2014, 1, 1, tzinfo=UTC))

  assert item is not None
  assert item.timestamp == datetime(2014, 4, 10, tzinfo=UTC)


def test_email_to_item_skips_unparseable_date():
  message = make_message()
  message.replace_header("Date", "not a date")
  skipped = []

  item = email_to_item(
    message,
    datetime(2014, 1, 1, tzinfo=UTC),
    on_skip_date=lambda path, date: skipped.append((path, date)),
  )

  assert item is None
  assert skipped == [(None, "not a date")]


def test_parse_email_datetime_supports_date_only_formats():
  assert parse_email_datetime("April 10, 2014") == datetime(2014, 4, 10, tzinfo=UTC)


def test_email_thread_id_prefers_in_reply_to():
  message = make_message()
  message["In-Reply-To"] = "<parent@example.test>"
  message["References"] = "<older@example.test> <newer@example.test>"

  item = email_to_item(message, datetime(2026, 1, 1, tzinfo=UTC))

  assert item is not None
  assert item.thread_id == "<parent@example.test>"


def test_email_thread_id_falls_back_to_last_reference():
  message = make_message()
  message["References"] = "<older@example.test> <newer@example.test>"

  item = email_to_item(message, datetime(2026, 1, 1, tzinfo=UTC))

  assert item is not None
  assert item.thread_id == "<newer@example.test>"


def test_parse_emlx_uses_length_prefix(tmp_path):
  raw_message = make_message().as_bytes()
  emlx = tmp_path / "message.emlx"
  emlx.write_bytes(str(len(raw_message)).encode() + b"\n" + raw_message + b"\n{}")

  parsed = parse_emlx(emlx)

  assert parsed["Message-ID"] == "<msg-1@example.test>"
  assert parsed.get_content().strip() == "Hei Alice, cabin plan is still on."


def test_email_adapter_counts_skipped_emlx_files(tmp_path):
  good = tmp_path / "good.emlx"
  bad = tmp_path / "bad.emlx"
  raw_message = make_message().as_bytes()
  good.write_bytes(str(len(raw_message)).encode() + b"\n" + raw_message)
  bad.write_text("not an emlx file")
  adapter = EmailAdapter([{"type": "emlx", "path": str(tmp_path)}])

  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))

  assert len(items) == 1
  assert adapter.skipped_emlx == 1


def test_email_adapter_counts_invalid_dates(tmp_path):
  bad = make_message()
  bad.replace_header("Date", "not a date")
  raw_message = bad.as_bytes()
  emlx = tmp_path / "bad-date.emlx"
  emlx.write_bytes(str(len(raw_message)).encode() + b"\n" + raw_message)
  adapter = EmailAdapter([{"type": "emlx", "path": str(tmp_path)}])

  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))

  assert items == []
  assert adapter.skipped_email_dates == 1


def test_strip_html_drops_blockquote_content():
  html = """
    <html><body>
      <p>My new reply.</p>
      <blockquote type="cite">
        <p>Original message that should not survive.</p>
      </blockquote>
    </body></html>
  """
  out = strip_html(html)
  assert "My new reply." in out
  assert "Original message" not in out


def test_strip_html_drops_gmail_quote_container():
  html = """
    <div>Top-level reply text.</div>
    <div class="gmail_quote">
      <div class="gmail_attr">On Tue, ... wrote:</div>
      <div>Quoted history we want gone.</div>
    </div>
  """
  out = strip_html(html)
  assert "Top-level reply text." in out
  assert "Quoted history" not in out
  assert "wrote:" not in out


def test_strip_html_drops_outlook_reply_container():
  html = """
    <div>Fresh reply body.</div>
    <div id="divRplyFwdMsg">
      <font>From: someone@example.com<br>Sent: ...<br>To: ...</font>
      <div>Older content.</div>
    </div>
  """
  out = strip_html(html)
  assert "Fresh reply body." in out
  assert "Older content" not in out
  assert "someone@example.com" not in out


def test_strip_html_drops_style_and_script_blocks():
  html = """
    <html>
      <head><style>.foo{color:red}</style></head>
      <body>
        <script>alert('x')</script>
        <p>Visible text.</p>
      </body>
    </html>
  """
  out = strip_html(html)
  assert "Visible text." in out
  assert "color:red" not in out
  assert "alert" not in out


def test_clean_email_body_strips_outlook_from_header_block():
  body = (
    "Hei, dette er svaret mitt.\n"
    "\n"
    "From: Alice <alice@example.test>\n"
    "Sent: Wednesday, April 29, 2026 12:00 PM\n"
    "To: Bob <bob@example.test>\n"
    "Subject: Cabin\n"
    "\n"
    "Original message text here.\n"
  )
  cleaned = clean_email_body(body)
  assert "dette er svaret mitt" in cleaned
  assert "alice@example.test" not in cleaned
  assert "Original message text here" not in cleaned


def test_clean_email_body_dedupes_repeated_quote_lines():
  body = (
    "New thought one.\n"
    "Some shared paragraph that appears in the quote chain too.\n"
    "New thought two.\n"
    "Some shared paragraph that appears in the quote chain too.\n"
    "Some shared paragraph that appears in the quote chain too.\n"
  )
  cleaned = clean_email_body(body)
  occurrences = cleaned.lower().count("some shared paragraph")
  assert occurrences == 1


def test_clean_email_body_strips_unsubscribe_and_disclaimer():
  body = (
    "Real content here.\n"
    "Click here to unsubscribe from this list.\n"
    "This email and any attachments are confidential and intended only for the addressee.\n"
    "If you received this in error, please notify the sender.\n"
  )
  cleaned = clean_email_body(body)
  assert "Real content here" in cleaned
  assert "unsubscribe" not in cleaned.lower()
  assert "confidential" not in cleaned.lower()


def test_clean_email_body_keeps_short_repeats():
  body = "Hi\nThanks for the update.\nHi\n"
  cleaned = clean_email_body(body)
  assert cleaned.count("Hi") == 2


def test_clean_email_body_collapses_long_tracking_urls():
  body = (
    "Read this update.\n"
    "https://post.eu.spmailtechnol.com/f/a/" + "x" * 200 + "\n"
    "More content here.\n"
  )
  cleaned = clean_email_body(body)
  assert "Read this update." in cleaned
  assert "More content here." in cleaned
  assert "spmailtechnol" not in cleaned


def test_extract_text_body_handles_html_in_text_plain_alternative():
  from email.message import EmailMessage

  msg = EmailMessage()
  msg["From"] = "marketing@example.test"
  msg["To"] = "you@example.test"
  msg["Date"] = "Wed, 29 Apr 2026 12:00:00 +0200"
  msg["Subject"] = "Newsletter"
  msg["Message-ID"] = "<news-1@example.test>"
  msg.set_content(
    "<!doctype html><html><head><style>.x{color:red}</style></head>"
    "<body><p>Real visible content.</p></body></html>"
  )

  item = email_to_item(msg, datetime(2026, 1, 1, tzinfo=UTC))
  assert item is not None
  assert "Real visible content" in item.content
  assert "<style>" not in item.content
  assert "<!doctype" not in item.content
  assert "color:red" not in item.content
