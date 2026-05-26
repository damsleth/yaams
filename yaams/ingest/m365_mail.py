"""Microsoft 365 mail ingester.

Shells out to `owa-mail` (which talks to Microsoft Graph via owa-piggy) to
list and fetch message bodies per profile. One yaams source per profile:
``mail_<profile>``.

Strategy:
  - Walk a date range in ``chunk_days`` slices to stay under owa-mail's
    200-result hard cap.
  - For each listed message, call ``owa-mail show --id <id>`` to fetch
    the full body (the listing endpoint only returns a short preview).
  - Walk both Inbox and SentItems by default so the user's outbound
    messages aren't lost.
  - Reuse ``clean_email_body`` / ``strip_html`` from email_mbox so the
    cleaned text matches what the mbox/emlx ingester produces.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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


@dataclass
class M365MailAdapter:
  profile: str
  folders: tuple[str, ...] = DEFAULT_FOLDERS
  user_addresses: list[str] = field(default_factory=list)
  skip_newsletters: bool = True
  chunk_days: int = DEFAULT_CHUNK_DAYS
  skipped_newsletters: int = field(default=0, init=False)
  skipped_email_dates: int = field(default=0, init=False)
  skipped_empty: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_newsletters = 0
    self.skipped_email_dates = 0
    self.skipped_empty = 0
    cutoff = ensure_utc(since).date()
    today = date.today()
    user_set = {a.strip().lower() for a in self.user_addresses if a}
    for folder in self.folders:
      chunk_start = cutoff
      while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=self.chunk_days - 1), today)
        for envelope in self._list(folder, chunk_start, chunk_end):
          item = self._materialize(envelope, folder, user_set)
          if item is not None:
            yield item
        chunk_start = chunk_end + timedelta(days=1)

  def _list(self, folder: str, start: date, end: date) -> list[dict]:
    result = subprocess.run(
      ["owa-mail", "messages",
       "--profile", self.profile,
       "--folder", folder,
       "--since", str(start),
       "--until", str(end),
       "--limit", "200"],
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

  def _fetch_body(self, message_id: str) -> dict | None:
    result = subprocess.run(
      ["owa-mail", "show", "--profile", self.profile, "--id", message_id],
      capture_output=True, text=True,
    )
    if result.returncode != 0:
      logger.warning(
        "owa-mail show failed (profile=%s id=%s rc=%d): %s",
        self.profile, message_id, result.returncode,
        (result.stderr or "").strip() or "no stderr",
      )
      return None
    if not result.stdout.strip():
      return None
    try:
      data = json.loads(result.stdout)
    except json.JSONDecodeError:
      logger.warning(
        "owa-mail show returned non-JSON (profile=%s id=%s)",
        self.profile, message_id,
      )
      return None
    return data if isinstance(data, dict) else None

  def _materialize(
    self, envelope: dict, folder: str, user_set: set[str],
  ) -> Item | None:
    sender = (envelope.get("from") or "").strip()
    sender_lower = sender.lower()
    is_outbound = sender_lower in user_set
    if self.skip_newsletters and not is_outbound:
      if is_automated_sender(sender) or _is_owa_newsletter(envelope):
        self.skipped_newsletters += 1
        return None

    message_id = envelope.get("id") or ""
    if not message_id:
      return None

    body_data = self._fetch_body(message_id)
    if body_data is None:
      self.skipped_empty += 1
      return None
    return _to_item(body_data, envelope, folder, self.profile)


def _is_owa_newsletter(envelope: dict) -> bool:
  """Best-effort newsletter detection from the listing payload.

  owa-mail's listing doesn't expose List-Unsubscribe headers, so we lean
  on sender patterns and subject keywords. The mbox path uses real RFC
  headers via is_newsletter() — this is the lossy equivalent.
  """
  subject = envelope.get("subject") or ""
  if _AUTOMATED_SUBJECT.search(subject):
    return True
  return False


def _to_item(
  body_data: dict, envelope: dict, folder: str, profile: str,
) -> Item | None:
  ts_str = body_data.get("received") or envelope.get("received") or ""
  if not ts_str:
    return None
  try:
    timestamp = parse_iso_datetime(ts_str)
  except ValueError:
    return None

  body_raw = body_data.get("body") or ""
  body_type = (body_data.get("body_type") or "").lower()
  if body_type == "html":
    body_text = strip_html(body_raw)
  else:
    body_text = body_raw
  body = clean_email_body(body_text.strip())
  if not body:
    return None
  if len(body) > MAX_EMAIL_CHARS:
    body = body[:MAX_EMAIL_CHARS]

  sender = (body_data.get("from") or envelope.get("from") or "unknown").strip()
  to_field = body_data.get("to") or envelope.get("to") or ""
  cc_field = body_data.get("cc") or envelope.get("cc") or ""
  bcc_field = body_data.get("bcc") or envelope.get("bcc") or ""
  to = _split_addresses(to_field)
  cc = _split_addresses(cc_field)
  bcc = _split_addresses(bcc_field)

  subject = (body_data.get("subject") or envelope.get("subject") or "").strip()
  message_id = body_data.get("id") or envelope.get("id") or ""
  conversation_id = body_data.get("conversation_id") or envelope.get("conversation_id")

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
      "has_attachments": bool(body_data.get("has_attachments", False)),
      "importance": body_data.get("importance", ""),
      "is_read": bool(body_data.get("is_read", False)),
      "web_link": body_data.get("web_link", ""),
    },
  )


def _split_addresses(value: str) -> list[str]:
  if not value:
    return []
  return [part.strip() for part in value.split(",") if part.strip()]
