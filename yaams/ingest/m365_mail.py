"""Microsoft 365 mail ingester.

Shells out to `owa-mail` (which talks to Microsoft Graph via owa-piggy) to
list message bodies per profile. One yaams source per profile:
``mail_<profile>``.

Strategy:
  - One `owa-mail messages --all --with-body` call per folder follows
    Graph's ``@odata.nextLink`` until the date window is exhausted, so a
    profile costs O(folders) subprocess hops regardless of message count.
    ``--all`` removes the old 200-result cap that forced date chunking
    (and silently dropped anything past 200 per chunk).
  - ``--with-body`` returns the full body + InternetMessageHeaders inline,
    so there is no per-message `show` roundtrip.
  - Walk both Inbox and SentItems by default so the user's outbound
    messages aren't lost.
  - Apply real RFC newsletter heuristics (List-Unsubscribe / List-Id /
    Precedence / Auto-Submitted) against the listing headers, matching
    `is_newsletter` from the mbox path. Falls back to sender / subject
    heuristics for messages without selectable headers.
  - Reuse ``clean_email_body`` / ``strip_html`` from email_mbox so the
    cleaned text matches what the mbox/emlx ingester produces.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator

logger = logging.getLogger("yaams.ingest.m365_mail")

from yaams.ingest.base import Item, hash_id
from yaams.ingest.email_mbox import (
  MAX_EMAIL_CHARS,
  clean_email_body,
  is_automated_sender,
  strip_html,
)
from yaams.time import ensure_utc, parse_iso_datetime

DEFAULT_FOLDERS = ("Inbox", "SentItems")
DEFAULT_CHUNK_DAYS = 30

_AUTOMATED_SUBJECT = re.compile(
  r"(unsubscribe|newsletter|digest|weekly update|do not reply)",
  re.IGNORECASE,
)

# RFC headers that mark a message as bulk/automated. Matches
# email_mbox.is_newsletter() but reads from owa-mail's internet_headers
# list (Graph's InternetMessageHeaders) instead of an email.Message.
_NEWSLETTER_HEADER_NAMES = frozenset({
  "list-unsubscribe", "list-id", "list-help",
})
_BULK_PRECEDENCES = frozenset({"bulk", "list", "junk"})


@dataclass
class M365MailAdapter:
  profile: str
  folders: tuple[str, ...] = DEFAULT_FOLDERS
  user_addresses: list[str] = field(default_factory=list)
  skip_newsletters: bool = True
  # Retained for config/back-compat; ``--all`` paginates server-side so
  # the adapter no longer slices the window into chunks.
  chunk_days: int = DEFAULT_CHUNK_DAYS
  skipped_newsletters: int = field(default=0, init=False)
  skipped_email_dates: int = field(default=0, init=False)
  skipped_empty: int = field(default=0, init=False)
  skipped_no_timestamp: int = field(default=0, init=False)
  # High-water timestamp of every message *scanned* (including ones we
  # skip as newsletters/empty). The ingest loop advances the watermark to
  # this so an all-skip profile stops re-walking its whole window forever.
  scanned_through: datetime | None = field(default=None, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_newsletters = 0
    self.skipped_email_dates = 0
    self.skipped_empty = 0
    self.skipped_no_timestamp = 0
    self.scanned_through = None
    cutoff = ensure_utc(since).date()
    today = date.today()
    if cutoff > today:
      return
    user_set = {a.strip().lower() for a in self.user_addresses if a}
    for folder in self.folders:
      for message in self._list(folder, cutoff, today):
        item = self._materialize(message, folder, user_set)
        if item is not None:
          yield item

  def _list(self, folder: str, start: date, end: date) -> list[dict]:
    """Bulk-fetch a folder's listings with body + headers inline.

    ``--all`` follows ``@odata.nextLink`` until the window is exhausted and
    ``--with-body`` inlines body + InternetMessageHeaders, so one subprocess
    per folder replaces both the per-chunk loop and any per-message `show`
    fan-out. Each entry has the same shape as `owa-mail show` output.
    """
    result = subprocess.run(
      ["owa-mail", "messages",
       "--profile", self.profile,
       "--folder", folder,
       "--since", str(start),
       "--until", str(end),
       "--limit", "200",
       "--all",
       "--with-body"],
      capture_output=True, text=True,
    )
    if result.returncode != 0:
      logger.warning(
        "owa-mail messages failed (profile=%s folder=%s rc=%d): %s",
        self.profile, folder, result.returncode,
        (result.stderr or "").strip() or "no stderr",
      )
      return []
    if not result.stdout.strip():
      return []
    try:
      data = json.loads(result.stdout)
    except json.JSONDecodeError:
      logger.warning(
        "owa-mail messages returned non-JSON (profile=%s folder=%s)",
        self.profile, folder,
      )
      return []
    return data if isinstance(data, list) else []

  def _materialize(
    self, message: dict, folder: str, user_set: set[str],
  ) -> Item | None:
    # Track the high-water timestamp of everything we scan, before any
    # skip decision, so the watermark can advance past skipped messages.
    ts_str = message.get("received") or ""
    if ts_str:
      try:
        ts = parse_iso_datetime(ts_str)
      except ValueError:
        ts = None
      if ts is not None and (self.scanned_through is None or ts > self.scanned_through):
        self.scanned_through = ts

    sender = (message.get("from") or "").strip()
    sender_lower = sender.lower()
    is_outbound = sender_lower in user_set
    if self.skip_newsletters and not is_outbound:
      if is_automated_sender(sender) or _is_owa_newsletter(message):
        self.skipped_newsletters += 1
        return None

    message_id = message.get("id") or ""
    if not message_id:
      self.skipped_empty += 1
      return None

    item = _to_item(message, folder, self.profile)
    if item is None:
      # _to_item returns None for missing timestamp or empty body after
      # cleaning. Track both so the perf cost of these silent drops is
      # visible in stats — the previous design hid them entirely.
      if not (message.get("received") or ""):
        self.skipped_no_timestamp += 1
      else:
        self.skipped_empty += 1
    return item


def _is_owa_newsletter(message: dict) -> bool:
  """Detect newsletters using the same signals as email_mbox.is_newsletter().

  Prefers RFC headers from `internet_headers` (`--with-body` populates
  this); falls back to sender / subject heuristics when headers are
  absent. Mirrors `is_newsletter` in email_mbox.py.
  """
  headers = message.get("internet_headers") or []
  for h in headers:
    name = (h.get("name") or "").lower()
    value = (h.get("value") or "").strip().lower()
    if not name:
      continue
    if name in _NEWSLETTER_HEADER_NAMES and value:
      return True
    if name == "precedence" and value in _BULK_PRECEDENCES:
      return True
    if name == "auto-submitted" and value and value != "no":
      return True
  subject = message.get("subject") or ""
  if _AUTOMATED_SUBJECT.search(subject):
    return True
  return False


def _to_item(message: dict, folder: str, profile: str) -> Item | None:
  ts_str = message.get("received") or ""
  if not ts_str:
    return None
  try:
    timestamp = parse_iso_datetime(ts_str)
  except ValueError:
    return None

  body_raw = message.get("body") or ""
  body_type = (message.get("body_type") or "").lower()
  if body_type == "html":
    body_text = strip_html(body_raw)
  else:
    body_text = body_raw
  body = clean_email_body(body_text.strip())
  if not body:
    return None
  if len(body) > MAX_EMAIL_CHARS:
    body = body[:MAX_EMAIL_CHARS]

  sender = (message.get("from") or "unknown").strip()
  to = _split_addresses(message.get("to") or "")
  cc = _split_addresses(message.get("cc") or "")
  bcc = _split_addresses(message.get("bcc") or "")

  subject = (message.get("subject") or "").strip()
  message_id = message.get("id") or ""
  conversation_id = message.get("conversation_id")

  source = f"mail_{profile}"
  return Item(
    id=hash_id(source, message_id),
    source=source,
    source_id=message_id,
    timestamp=timestamp,
    sender=sender,
    recipients=to + cc,
    content=body,
    subject=subject,
    thread_id=conversation_id,
    raw_metadata={
      "profile": profile,
      "folder": folder,
      "cc": cc,
      "bcc": bcc,
      "has_attachments": bool(message.get("has_attachments", False)),
      "importance": message.get("importance", ""),
      "is_read": bool(message.get("is_read", False)),
      "web_link": message.get("web_link", ""),
    },
  )


def _split_addresses(value: str) -> list[str]:
  if not value:
    return []
  return [part.strip() for part in value.split(",") if part.strip()]
