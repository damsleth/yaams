from __future__ import annotations

from datetime import UTC, datetime

from yaams.ingest.calendar import _to_item


def _event(start: str) -> dict:
  return {
    "id": "evt-1",
    "subject": "Standup",
    "start": start,
    "end": start,
    "showAs": "busy",
    "isAllDay": False,
    "location": "",
    "categories": [],
  }


def test_to_item_converts_offset_aware_timestamp_to_utc():
  item = _to_item(_event("2026-05-04T09:00:00+02:00"), profile="work")
  assert item is not None
  assert item.timestamp == datetime(2026, 5, 4, 7, 0, tzinfo=UTC)


def test_to_item_handles_z_suffix_as_utc():
  item = _to_item(_event("2026-05-04T09:00:00Z"), profile="work")
  assert item is not None
  assert item.timestamp == datetime(2026, 5, 4, 9, 0, tzinfo=UTC)


def test_to_item_treats_naive_timestamp_as_utc():
  item = _to_item(_event("2026-05-04T09:00:00"), profile="work")
  assert item is not None
  assert item.timestamp == datetime(2026, 5, 4, 9, 0, tzinfo=UTC)


def test_to_item_returns_none_for_invalid_timestamp():
  assert _to_item(_event("not-a-timestamp"), profile="work") is None
