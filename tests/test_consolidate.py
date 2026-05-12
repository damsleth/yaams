from __future__ import annotations

from datetime import UTC, datetime, timedelta

from yaams.consolidate import (
  CONSOLIDATOR_VERSION,
  SessionConfig,
  build_consolidations,
  iter_sessions,
)
from yaams.ingest.base import Item, hash_id


def make_item(
  source: str = "imessage",
  thread_id: str = "thread-1",
  sender: str = "alice@example.test",
  content: str = "hi",
  ts: datetime | None = None,
  msg_id: str = "msg",
) -> Item:
  if ts is None:
    ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  return Item(
    id=hash_id(source, f"{thread_id}:{msg_id}"),
    source=source,
    source_id=f"{thread_id}:{msg_id}",
    timestamp=ts,
    sender=sender,
    recipients=[],
    content=content,
    subject="",
    thread_id=thread_id,
  )


def test_iter_sessions_splits_on_gap():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(msg_id="a", ts=base),
    make_item(msg_id="b", ts=base + timedelta(minutes=10)),
    make_item(msg_id="c", ts=base + timedelta(hours=8)),
    make_item(msg_id="d", ts=base + timedelta(hours=8, minutes=5)),
  ]
  sessions = list(iter_sessions(items))
  assert len(sessions) == 2
  assert [len(s.items) for s in sessions] == [2, 2]


def test_iter_sessions_caps_max_items():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(msg_id=f"m{i}", ts=base + timedelta(minutes=i))
    for i in range(105)
  ]
  cfg = SessionConfig(max_session_items=50)
  sessions = list(iter_sessions(items, cfg))
  assert [len(s.items) for s in sessions] == [50, 50, 5]


def test_iter_sessions_keeps_threads_separate():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(thread_id="A", msg_id="a", ts=base),
    make_item(thread_id="B", msg_id="b", ts=base + timedelta(minutes=1)),
    make_item(thread_id="A", msg_id="c", ts=base + timedelta(minutes=2)),
  ]
  sessions = list(iter_sessions(items))
  thread_ids = sorted(s.thread_id for s in sessions)
  assert thread_ids == ["A", "B"]


def test_iter_sessions_keeps_sources_separate():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(source="imessage", thread_id="t", msg_id="a", ts=base),
    make_item(source="teams_work", thread_id="t", msg_id="b", ts=base + timedelta(minutes=1)),
  ]
  sessions = list(iter_sessions(items))
  assert {s.source for s in sessions} == {"imessage", "teams_work"}


def test_build_consolidations_skips_small_sessions():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(msg_id="a", ts=base),
    make_item(msg_id="b", ts=base + timedelta(minutes=1)),
  ]
  consolidations = build_consolidations(items, SessionConfig(min_session_items=3))
  assert consolidations == []


def test_build_consolidations_consolidates_long_session():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(
      msg_id=f"m{i}",
      sender="alice@example.test" if i % 2 == 0 else "user@example.test",
      content=f"message {i}",
      ts=base + timedelta(minutes=i),
    )
    for i in range(8)
  ]
  consolidations = build_consolidations(items)
  assert len(consolidations) == 1
  c = consolidations[0]
  assert c.item_count == 8
  assert c.consolidator_version == CONSOLIDATOR_VERSION
  assert sorted(c.participants) == sorted(["alice@example.test", "user@example.test"])
  assert "message 0" in c.summary
  assert "message 7" in c.summary
  assert c.start_timestamp == items[0].timestamp
  assert c.end_timestamp == items[-1].timestamp


def test_consolidation_id_stable_across_runs():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(msg_id=f"m{i}", ts=base + timedelta(minutes=i))
    for i in range(5)
  ]
  a = build_consolidations(items)[0]
  b = build_consolidations(items)[0]
  assert a.id == b.id


def test_build_summary_truncates_at_limit():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  big_text = "x" * 200
  items = [
    make_item(msg_id=f"m{i}", content=big_text, ts=base + timedelta(minutes=i))
    for i in range(100)
  ]
  consolidations = build_consolidations(items, SessionConfig(summary_max_chars=2000, max_session_items=200))
  assert len(consolidations) == 1
  summary = consolidations[0].summary
  assert len(summary) <= 2200
  assert "more messages" in summary


def test_build_summary_single_session_single_day_header():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    make_item(msg_id=f"m{i}", ts=base + timedelta(minutes=i)) for i in range(3)
  ]
  consolidations = build_consolidations(items)
  assert "2026-04-01" in consolidations[0].summary
  assert " to " not in consolidations[0].summary.split(":")[0]


def test_participants_deduplicate_preserve_order():
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  senders = ["alice@example.test", "user@example.test", "alice@example.test", "user@example.test"]
  items = [
    make_item(msg_id=f"m{i}", sender=senders[i], ts=base + timedelta(minutes=i))
    for i in range(4)
  ]
  consolidations = build_consolidations(items)
  assert consolidations[0].participants == ["alice@example.test", "user@example.test"]
