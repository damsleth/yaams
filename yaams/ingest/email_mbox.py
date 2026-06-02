from __future__ import annotations

import email
import email.errors
import email.utils
import hashlib
import mailbox
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc

MAX_EMAIL_CHARS = 20_000

_QUOTE_LINE = re.compile(r"^>+.*$", re.MULTILINE)
_ON_WROTE = re.compile(
  r"\n?-{2,}.*\n?On .{0,200}wrote:\s*\n?",
  re.DOTALL,
)
_FORWARDED = re.compile(
  r"\n?-{3,}\s*(Forwarded|Original|Begin forwarded) (message|Message)\s*-{0,3}.*",
  re.DOTALL | re.IGNORECASE,
)
_FROM_HEADER_BLOCK = re.compile(
  r"\n?(From|Fra):\s.{0,400}?\n(Sent|Date|Sendt|Til|To):\s.*",
  re.DOTALL | re.IGNORECASE,
)
_SIG_BOUNDARY = re.compile(r"\n--\s*\n.*", re.DOTALL)
_SIG_KEYWORDS = re.compile(
  r"\n(Best regards|Kind regards|Med vennlig hilsen|Vennlig hilsen|Mvh\b|"
  r"Sent from my \w+|Get Outlook for \w+)[ ,!.]*\n.*",
  re.DOTALL | re.IGNORECASE,
)
_UNSUBSCRIBE = re.compile(
  r"\n[^\n]*(unsubscribe|view (this|the) (email|message) in (your|a) browser|"
  r"avregistrer|meld deg av|click here to (opt[- ]?out|unsubscribe))[^\n]*",
  re.IGNORECASE,
)
_DISCLAIMER = re.compile(
  r"\n[^\n]*(this (e[- ]?mail|message) (and|with) any attachments? (is|are|may be) "
  r"(confidential|privileged)|denne e[- ]?posten[^\n]{0,40}konfidensiel|"
  r"if you (are|have received) this (e[- ]?mail|message) in error)[^\n]*.*",
  re.DOTALL | re.IGNORECASE,
)
_TRACKING_URL_LINE = re.compile(
  r"^[\(\[\s]*https?://[^\s]*?(click|track|mail|email|ses|mandrill|sendgrid|mailchimp|hubspot|"
  r"campaign-archive|list-manage|ablink|spmailtechnol|sparkpostmail|sendlane|"
  r"convertkit|substackcdn|emltrk|mkt[0-9]+\.com|customeriomail)[^\s]*[\s\)\]]*$",
  re.IGNORECASE | re.MULTILINE,
)
_LONG_URL = re.compile(r"https?://[^\s]{120,}")
_WHITESPACE_RUN = re.compile(r"[ \t]+")
_BLANK_LINE_RUN = re.compile(r"\n\s*\n+")

_AUTOMATED_LOCAL_PARTS = re.compile(
  r"^(no[-_]?reply|do[-_]?not[-_]?reply|donotreply|automated|"
  r"notifications?|invitations?|informer|newsletter|mailer|bouncer?|"
  r"reply|alerts?|updates?|digest|ikkesvar|noresponse|noresponder|"
  r"postmaster|mailer-daemon|notify|notification|hello|team|news|"
  r"announce|automatic|system)@",
  re.IGNORECASE,
)


def is_automated_sender(sender: str) -> bool:
  return bool(_AUTOMATED_LOCAL_PARTS.match(sender or ""))


def is_newsletter(message: Message) -> bool:
  if message.get("List-Unsubscribe") or message.get("List-Id") or message.get("List-Help"):
    return True
  precedence = (message.get("Precedence") or "").strip().lower()
  if precedence in {"bulk", "list", "junk"}:
    return True
  auto = (message.get("Auto-Submitted") or "").strip().lower()
  if auto and auto != "no":
    return True
  return False


def _dedupe_lines(text: str, min_len: int = 25) -> str:
  seen: set[str] = set()
  out: list[str] = []
  for line in text.splitlines():
    key = _WHITESPACE_RUN.sub(" ", line).strip().lower()
    if not key:
      out.append(line)
      continue
    if len(key) >= min_len and key in seen:
      continue
    seen.add(key)
    out.append(line)
  return "\n".join(out)


def clean_email_body(text: str) -> str:
  text = _FORWARDED.sub("", text)
  text = _FROM_HEADER_BLOCK.sub("", text)
  text = _ON_WROTE.sub("", text)
  text = _QUOTE_LINE.sub("", text)
  text = _SIG_BOUNDARY.sub("", text)
  text = _SIG_KEYWORDS.sub("", text)
  text = _DISCLAIMER.sub("", text)
  text = _UNSUBSCRIBE.sub("", text)
  text = _TRACKING_URL_LINE.sub("", text)
  text = _LONG_URL.sub("[link]", text)
  text = _BLANK_LINE_RUN.sub("\n", text)
  text = _dedupe_lines(text)
  lines = [_WHITESPACE_RUN.sub(" ", l).rstrip() for l in text.splitlines()]
  lines = [l for l in lines if l.strip()]
  return "\n".join(lines)


@dataclass
class EmailAdapter:
  sources: list[dict]
  user_addresses: list[str] = field(default_factory=list)
  skip_newsletters: bool = True
  skipped_emlx: int = field(default=0, init=False)
  skipped_email_dates: int = field(default=0, init=False)
  skipped_newsletters: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_emlx = 0
    self.skipped_email_dates = 0
    self.skipped_newsletters = 0
    cutoff = ensure_utc(since)
    user_set = {a.strip().lower() for a in self.user_addresses if a}
    for source in self.sources:
      source_type = source.get("type")
      path = expand_path(source["path"])
      if source_type == "mbox":
        yield from extract_mbox(
          path,
          cutoff,
          on_skip_date=self._record_date_skip,
          user_addresses=user_set,
          skip_newsletters=self.skip_newsletters,
          on_skip_newsletter=self._record_newsletter_skip,
        )
      elif source_type == "emlx":
        yield from extract_emlx_tree(
          path,
          cutoff,
          on_skip=self._record_emlx_skip,
          on_skip_date=self._record_date_skip,
          user_addresses=user_set,
          skip_newsletters=self.skip_newsletters,
          on_skip_newsletter=self._record_newsletter_skip,
        )
      else:
        raise ValueError(f"Unsupported email source type: {source_type}")

  def _record_emlx_skip(self, path: Path, error: Exception) -> None:
    self.skipped_emlx += 1

  def _record_date_skip(self, path: Path | None, date_header: str) -> None:
    self.skipped_email_dates += 1

  def _record_newsletter_skip(self, path: Path | None, sender: str) -> None:
    self.skipped_newsletters += 1


def extract_mbox(
  path: Path,
  since: datetime,
  on_skip_date: Callable[[Path | None, str], None] | None = None,
  user_addresses: set[str] | None = None,
  skip_newsletters: bool = True,
  on_skip_newsletter: Callable[[Path | None, str], None] | None = None,
) -> Iterator[Item]:
  mbox = mailbox.mbox(path)
  for message in mbox:
    item = email_to_item(
      message,
      since,
      on_skip_date=on_skip_date,
      user_addresses=user_addresses,
      skip_newsletters=skip_newsletters,
      on_skip_newsletter=on_skip_newsletter,
    )
    if item is not None:
      yield item


def extract_emlx_tree(
  path: Path,
  since: datetime,
  on_skip: Callable[[Path, Exception], None] | None = None,
  on_skip_date: Callable[[Path | None, str], None] | None = None,
  user_addresses: set[str] | None = None,
  skip_newsletters: bool = True,
  on_skip_newsletter: Callable[[Path | None, str], None] | None = None,
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
      user_addresses=user_addresses,
      skip_newsletters=skip_newsletters,
      on_skip_newsletter=on_skip_newsletter,
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
  user_addresses: set[str] | None = None,
  skip_newsletters: bool = True,
  on_skip_newsletter: Callable[[Path | None, str], None] | None = None,
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

  sender = _first_address(message.get("From", "")) or "unknown"
  sender_lower = sender.lower()
  user_set = user_addresses or set()
  if skip_newsletters and sender_lower not in user_set:
    if is_newsletter(message) or is_automated_sender(sender):
      if on_skip_newsletter is not None:
        on_skip_newsletter(fallback_path, sender)
      return None

  body = clean_email_body(extract_text_body(message).strip())
  if not body:
    return None
  if len(body) > MAX_EMAIL_CHARS:
    body = body[:MAX_EMAIL_CHARS]

  message_id = str(message.get("Message-ID") or generate_fallback_id(
    message,
    body,
    fallback_path=fallback_path,
  )).strip()
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


_HTML_SHAPED = re.compile(
  r"<!doctype\s+html|<html[\s>]|<body[\s>]|<style[\s>]|<head[\s>]",
  re.IGNORECASE,
)


def _looks_like_html(text: str) -> bool:
  sample = text[:2000]
  if _HTML_SHAPED.search(sample):
    return True
  if sample.count("<") + sample.count(">") > 40 and sample.count("</") > 5:
    return True
  return False


def extract_text_body(message: Message) -> str:
  if message.is_multipart():
    plain = _first_part(message, "text/plain")
    if plain and not _looks_like_html(plain):
      return plain
    html = _first_part(message, "text/html")
    if html:
      return strip_html(html)
    if plain:
      return strip_html(plain)
    return ""
  content_type = message.get_content_type()
  payload = _decode_message_part(message)
  if content_type == "text/html" or _looks_like_html(payload):
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
  if not isinstance(payload, (bytes, bytearray)):
    raw_payload = message.get_payload()
    return raw_payload if isinstance(raw_payload, str) else ""
  charset = message.get_content_charset() or "utf-8"
  return bytes(payload).decode(charset, errors="replace")


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


_HTML_DROP_TAGS = {"style", "script", "head", "title", "meta", "link"}
_HTML_QUOTE_TAGS = {"blockquote"}
_HTML_QUOTE_CLASS_PREFIXES = (
  "gmail_quote",
  "gmail_attr",
  "gmail_extra",
  "yahoo_quoted",
  "moz-cite-prefix",
  "OutlookMessageHeader",
)
_HTML_QUOTE_IDS = {
  "divRplyFwdMsg",
  "appendonsend",
  "reply-intro",
}


def _attr_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
  return {k: (v or "") for k, v in attrs}


def _is_quote_container(tag: str, attrs: dict[str, str]) -> bool:
  if tag in _HTML_QUOTE_TAGS:
    return True
  cls = attrs.get("class", "")
  if cls and any(cls.startswith(prefix) or f" {prefix}" in f" {cls}" for prefix in _HTML_QUOTE_CLASS_PREFIXES):
    return True
  if attrs.get("id", "") in _HTML_QUOTE_IDS:
    return True
  return False


class _HTMLTextExtractor(HTMLParser):
  def __init__(self):
    super().__init__()
    self.parts: list[str] = []
    self._drop_depth = 0
    self._quote_depth = 0
    self._stack: list[str] = []

  def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
    self._stack.append(tag)
    if tag in _HTML_DROP_TAGS:
      self._drop_depth += 1
      return
    if _is_quote_container(tag, _attr_dict(attrs)):
      self._quote_depth += 1
      self._stack[-1] = f"__quote__{tag}"

  def handle_endtag(self, tag: str) -> None:
    if not self._stack:
      return
    last = self._stack.pop()
    if last == f"__quote__{tag}":
      if self._quote_depth > 0:
        self._quote_depth -= 1
    elif tag in _HTML_DROP_TAGS:
      if self._drop_depth > 0:
        self._drop_depth -= 1

  def handle_data(self, data: str) -> None:
    if self._drop_depth > 0 or self._quote_depth > 0:
      return
    stripped = data.strip()
    if stripped:
      self.parts.append(stripped)

  def text(self) -> str:
    return "\n".join(self.parts)
