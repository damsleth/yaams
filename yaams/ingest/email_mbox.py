from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import email
from email import policy
from email.message import EmailMessage, Message
import email.utils
from html.parser import HTMLParser
from pathlib import Path
import hashlib
import mailbox
from typing import Callable, Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc


MAX_EMAIL_CHARS = 50_000


@dataclass
class EmailAdapter:
  sources: list[dict]
  skipped_emlx: int = field(default=0, init=False)
  skipped_email_dates: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_emlx = 0
    self.skipped_email_dates = 0
    cutoff = ensure_utc(since)
    for source in self.sources:
      source_type = source.get("type")
      path = expand_path(source["path"])
      if source_type == "mbox":
        yield from extract_mbox(path, cutoff, on_skip_date=self._record_date_skip)
      elif source_type == "emlx":
        yield from extract_emlx_tree(
          path,
          cutoff,
          on_skip=self._record_emlx_skip,
          on_skip_date=self._record_date_skip,
        )
      else:
        raise ValueError(f"Unsupported email source type: {source_type}")

  def _record_emlx_skip(self, path: Path, error: Exception) -> None:
    self.skipped_emlx += 1

  def _record_date_skip(self, path: Path | None, date_header: str) -> None:
    self.skipped_email_dates += 1


def extract_mbox(
  path: Path,
  since: datetime,
  on_skip_date: Callable[[Path | None, str], None] | None = None,
) -> Iterator[Item]:
  mbox = mailbox.mbox(path)
  for message in mbox:
    item = email_to_item(message, since, on_skip_date=on_skip_date)
    if item is not None:
      yield item


def extract_emlx_tree(
  path: Path,
  since: datetime,
  on_skip: Callable[[Path, Exception], None] | None = None,
  on_skip_date: Callable[[Path | None, str], None] | None = None,
) -> Iterator[Item]:
  files = [path] if path.is_file() else sorted(path.rglob("*.emlx"))
  for emlx_path in files:
    try:
      message = parse_emlx(emlx_path)
    except (OSError, ValueError, email.errors.MessageError) as exc:
      if on_skip is not None:
        on_skip(emlx_path, exc)
      continue
    item = email_to_item(
      message,
      since,
      fallback_path=emlx_path,
      on_skip_date=on_skip_date,
    )
    if item is not None:
      yield item


def parse_emlx(path: Path) -> EmailMessage:
  raw = path.read_bytes()
  first_newline = raw.index(b"\n")
  byte_count = int(raw[:first_newline])
  email_bytes = raw[first_newline + 1 : first_newline + 1 + byte_count]
  return email.message_from_bytes(email_bytes, policy=policy.default)


def email_to_item(
  message: Message,
  since: datetime,
  fallback_path: Path | None = None,
  on_skip_date: Callable[[Path | None, str], None] | None = None,
) -> Item | None:
  date_header = message.get("Date")
  if not date_header:
    if on_skip_date is not None:
      on_skip_date(fallback_path, str(date_header or ""))
    return None
  timestamp = parse_email_datetime(date_header)
  if timestamp is None:
    if on_skip_date is not None:
      on_skip_date(fallback_path, str(date_header))
    return None
  if timestamp < ensure_utc(since):
    return None

  body = extract_text_body(message).strip()
  if not body:
    return None
  if len(body) > MAX_EMAIL_CHARS:
    body = body[:MAX_EMAIL_CHARS]

  message_id = str(message.get("Message-ID") or generate_fallback_id(
    message,
    body,
    fallback_path=fallback_path,
  )).strip()
  sender = _first_address(message.get("From", "")) or "unknown"
  to = _addresses(message.get_all("To", []))
  cc = _addresses(message.get_all("Cc", []))
  bcc = _addresses(message.get_all("Bcc", []))
  thread_id = _thread_id(message)
  subject = str(message.get("Subject", ""))

  return Item(
    id=hash_id("email", message_id),
    source="email",
    source_id=message_id,
    timestamp=timestamp,
    sender=sender,
    recipients=to + cc,
    content=body,
    subject=subject,
    thread_id=thread_id,
    raw_metadata={
      "cc": cc,
      "bcc": bcc,
      "reply_to": message.get("Reply-To"),
    },
  )


def parse_email_datetime(date_header: str) -> datetime | None:
  try:
    return ensure_utc(email.utils.parsedate_to_datetime(date_header))
  except (TypeError, ValueError):
    pass

  normalized = " ".join(str(date_header).strip().split())
  for fmt in [
    "%A, %B %d, %Y",
    "%A, %b %d, %Y",
    "%a, %B %d, %Y",
    "%a, %b %d, %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y-%m-%d",
  ]:
    try:
      return datetime.strptime(normalized, fmt).replace(tzinfo=UTC)
    except ValueError:
      continue
  return None


def extract_text_body(message: Message) -> str:
  if message.is_multipart():
    plain = _first_part(message, "text/plain")
    if plain:
      return plain
    html = _first_part(message, "text/html")
    return strip_html(html) if html else ""
  content_type = message.get_content_type()
  payload = _decode_message_part(message)
  if content_type == "text/html":
    return strip_html(payload)
  return payload


def strip_html(html: str) -> str:
  parser = _HTMLTextExtractor()
  parser.feed(html)
  parser.close()
  return parser.text()


def generate_fallback_id(
  message: Message,
  body: str,
  fallback_path: Path | None = None,
) -> str:
  basis = "\n".join(
    [
      str(fallback_path or ""),
      str(message.get("Date", "")),
      str(message.get("From", "")),
      str(message.get("To", "")),
      str(message.get("Subject", "")),
      body[:1000],
    ]
  )
  digest = hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()
  return f"fallback:{digest}"


def _first_part(message: Message, content_type: str) -> str:
  for part in message.walk():
    if part.is_multipart():
      continue
    if part.get_content_disposition() == "attachment":
      continue
    if part.get_content_type() == content_type:
      return _decode_message_part(part)
  return ""


def _decode_message_part(message: Message) -> str:
  if isinstance(message, EmailMessage):
    try:
      content = message.get_content()
      return content if isinstance(content, str) else str(content)
    except (LookupError, UnicodeDecodeError):
      pass

  payload = message.get_payload(decode=True)
  if payload is None:
    raw_payload = message.get_payload()
    return raw_payload if isinstance(raw_payload, str) else ""
  charset = message.get_content_charset() or "utf-8"
  return payload.decode(charset, errors="replace")


def _addresses(values: list[str]) -> list[str]:
  return [addr for _, addr in email.utils.getaddresses(values) if addr]


def _first_address(value: str) -> str:
  name, addr = email.utils.parseaddr(value)
  return addr or name


def _thread_id(message: Message) -> str | None:
  thread_id = message.get("In-Reply-To")
  references = message.get("References", "")
  if not thread_id and references:
    thread_id = references.split()[-1]
  return thread_id.strip() if thread_id else None


class _HTMLTextExtractor(HTMLParser):
  def __init__(self):
    super().__init__()
    self.parts: list[str] = []

  def handle_data(self, data: str) -> None:
    stripped = data.strip()
    if stripped:
      self.parts.append(stripped)

  def text(self) -> str:
    return "\n".join(self.parts)
