from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Iterator

from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc


@dataclass
class CalendarAdapter:
  profile: str
  skip_free: bool = True   # skip showAs=Free events (blockers, OOO placeholders)
  chunk_days: int = 90     # Graph API works best with bounded ranges

  def extract(self, since: datetime) -> Iterator[Item]:
    cutoff = ensure_utc(since).date()
    today = date.today()
    chunk_start = cutoff
    while chunk_start <= today:
      chunk_end = min(chunk_start + timedelta(days=self.chunk_days - 1), today)
      for event in self._fetch(chunk_start, chunk_end):
        if self.skip_free and (event.get("showAs") or "").lower() == "free":
          continue
        item = _to_item(event, self.profile)
        if item:
          yield item
      chunk_start = chunk_end + timedelta(days=1)

  def _fetch(self, start: date, end: date) -> list[dict]:
    result = subprocess.run(
      ["owa-cal", "events", "--profile", self.profile,
       "--from", str(start), "--to", str(end)],
      capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
      return []
    try:
      return json.loads(result.stdout)
    except json.JSONDecodeError:
      return []


def _to_item(event: dict, profile: str) -> Item | None:
  event_id = event.get("id") or ""
  start_str = event.get("start") or ""
  subject = (event.get("subject") or "").strip()
  if not subject or not start_str:
    return None

  content = subject
  if event.get("location"):
    content += f"\nLocation: {event['location']}"
  if event.get("categories"):
    content += f"\nCategories: {', '.join(event['categories'])}"

  try:
    ts = datetime.fromisoformat(start_str).replace(tzinfo=UTC)
  except ValueError:
    return None

  source = f"calendar_{profile}"
  return Item(
    id=hash_id(source, f"{event_id}:{start_str}"),
    source=source,
    source_id=f"{event_id}:{start_str}",
    timestamp=ts,
    sender="me",
    recipients=[],
    content=content,
    subject=subject,
    thread_id=None,
    raw_metadata={
      "profile": profile,
      "event_id": event_id,
      "end": event.get("end"),
      "is_all_day": event.get("isAllDay", False),
      "show_as": event.get("showAs", ""),
      "location": event.get("location", ""),
      "categories": event.get("categories", []),
    },
  )
