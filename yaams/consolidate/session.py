from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Iterable, Iterator, Sequence

from yaams.ingest.base import Item
from yaams.time import to_local

CONSOLIDATOR_VERSION = "session-1"
DEFAULT_GAP_MINUTES = 240
DEFAULT_MAX_SESSION_ITEMS = 50
DEFAULT_MIN_SESSION_ITEMS = 3
DEFAULT_SUMMARY_MAX_CHARS = 8_000


@dataclass
class SessionConfig:
  gap_minutes: int = DEFAULT_GAP_MINUTES
  max_session_items: int = DEFAULT_MAX_SESSION_ITEMS
  min_session_items: int = DEFAULT_MIN_SESSION_ITEMS
  summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS


@dataclass
class Session:
  source: str
  thread_id: str
  items: list[Item]

  @property
  def start_timestamp(self) -> datetime:
    return self.items[0].timestamp

  @property
  def end_timestamp(self) -> datetime:
    return self.items[-1].timestamp

  @property
  def participants(self) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for item in self.items:
      if item.sender and item.sender not in seen_set:
        seen.append(item.sender)
        seen_set.add(item.sender)
    return seen


@dataclass
class Consolidation:
  id: str
  source: str
  thread_id: str
  start_timestamp: datetime
  end_timestamp: datetime
  participants: list[str]
  item_count: int
  summary: str
  raw_item_ids: list[str]
  consolidator_version: str = CONSOLIDATOR_VERSION
  created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def iter_sessions(
  items: Sequence[Item],
  config: SessionConfig | None = None,
) -> Iterator[Session]:
  cfg = config or SessionConfig()
  gap = timedelta(minutes=cfg.gap_minutes)
  if not items:
    return
  ordered: list[Item] = sorted(items, key=lambda i: (i.source, i.thread_id or "", i.timestamp))
  current_key: tuple[str, str] | None = None
  bucket: list[Item] = []
  for item in ordered:
    key = (item.source, item.thread_id or "")
    if current_key is None:
      current_key = key
    if key != current_key:
      yield from _emit_sessions(bucket, gap, cfg)
      bucket = []
      current_key = key
    bucket.append(item)
  if bucket:
    yield from _emit_sessions(bucket, gap, cfg)


def _emit_sessions(
  bucket: list[Item],
  gap: timedelta,
  cfg: SessionConfig,
) -> Iterator[Session]:
  if not bucket:
    return
  source = bucket[0].source
  thread_id = bucket[0].thread_id or ""
  current: list[Item] = [bucket[0]]
  for prev, item in zip(bucket, bucket[1:]):
    if (
      item.timestamp - prev.timestamp > gap
      or len(current) >= cfg.max_session_items
    ):
      yield Session(source=source, thread_id=thread_id, items=current)
      current = [item]
    else:
      current.append(item)
  if current:
    yield Session(source=source, thread_id=thread_id, items=current)


def build_summary(session: Session, max_chars: int = DEFAULT_SUMMARY_MAX_CHARS) -> str:
  participants = ", ".join(session.participants) or "unknown"
  date_range = _format_date_range(session.start_timestamp, session.end_timestamp)
  header = f"{session.source} session {date_range} with {participants}:"
  budget = max(0, max_chars - len(header) - 1)
  lines: list[str] = []
  used = 0
  for item in session.items:
    rendered = _format_item_line(item)
    if used + len(rendered) + 1 > budget:
      remaining = len(session.items) - len(lines)
      if remaining > 0:
        lines.append(f"... and {remaining} more messages")
      break
    lines.append(rendered)
    used += len(rendered) + 1
  return "\n".join([header, *lines]).strip()


def _format_item_line(item: Item) -> str:
  ts = to_local(item.timestamp).strftime("%Y-%m-%d %H:%M")
  sender = item.sender or "unknown"
  content = (item.content or "").strip().replace("\n", " ")
  return f"[{ts}] {sender}: {content}"


def _format_date_range(start: datetime, end: datetime) -> str:
  start_local = to_local(start)
  end_local = to_local(end)
  if start_local.date() == end_local.date():
    return start_local.strftime("%Y-%m-%d")
  return f"{start_local.strftime('%Y-%m-%d')} to {end_local.strftime('%Y-%m-%d')}"


def build_consolidations(
  items: Iterable[Item],
  config: SessionConfig | None = None,
) -> list[Consolidation]:
  cfg = config or SessionConfig()
  out: list[Consolidation] = []
  for session in iter_sessions(list(items), cfg):
    if len(session.items) < cfg.min_session_items:
      continue
    out.append(_to_consolidation(session, cfg))
  return out


def _to_consolidation(session: Session, cfg: SessionConfig) -> Consolidation:
  summary = build_summary(session, max_chars=cfg.summary_max_chars)
  raw_ids = [item.id for item in session.items]
  cid = _consolidation_id(session, raw_ids)
  return Consolidation(
    id=cid,
    source=session.source,
    thread_id=session.thread_id,
    start_timestamp=session.start_timestamp,
    end_timestamp=session.end_timestamp,
    participants=session.participants,
    item_count=len(session.items),
    summary=summary,
    raw_item_ids=raw_ids,
  )


def _consolidation_id(session: Session, raw_ids: list[str]) -> str:
  basis = "|".join(
    [
      session.source,
      session.thread_id,
      session.start_timestamp.isoformat(),
      session.end_timestamp.isoformat(),
      str(len(raw_ids)),
      raw_ids[0] if raw_ids else "",
      raw_ids[-1] if raw_ids else "",
    ]
  )
  digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
  return f"cons:{digest[:32]}"


def consolidation_metadata(consolidation: Consolidation) -> str:
  return json.dumps(
    {
      "participants": consolidation.participants,
      "raw_item_ids": consolidation.raw_item_ids,
    },
    ensure_ascii=False,
  )
