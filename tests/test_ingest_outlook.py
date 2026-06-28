"""Outlook.app adapter — parsing + time conversion (no live Outlook needed)."""

from __future__ import annotations

from datetime import UTC, datetime

from yaams.ingest import outlook_app as oa
from yaams.ingest.outlook_app import (
  FS,
  RS,
  OutlookCalendarAdapter,
  OutlookMailAdapter,
  _local_to_utc,
  _parse_records,
)


def _cal_rec(ev_id, start, end, subj, org, loc, body):
  return FS.join([ev_id, start, end, subj, org, loc, body])


def _mail_rec(mid, ts, subj, sender, folder, body):
  return FS.join([mid, ts, subj, sender, folder, body])


def test_local_to_utc_roundtrips_and_rejects_junk():
  # A naive local wall-clock -> aware UTC. Compare against what Python's own
  # local-tz conversion produces so the test is tz-independent.
  expected = datetime(2026, 6, 23, 14, 30, 0).astimezone(UTC)
  assert _local_to_utc("2026:6:23:14:30:0") == expected
  assert _local_to_utc("") is None
  assert _local_to_utc("2026:13:99:0:0:0") is None  # invalid month/day


def test_parse_records_skips_short_and_empty():
  out = RS.join(["a" + FS + "b", "", "solo", "x" + FS + "y" + FS + "z"])
  recs = list(_parse_records(out, 2))
  assert recs == [["a", "b"], ["x", "y", "z"]]  # "" and "solo" dropped


def test_calendar_extract(monkeypatch):
  out = RS.join([
    _cal_rec("EV1", "2026:6:23:9:0:0", "2026:6:23:10:0:0", "Standup", "boss@x.io", "Room 4", "agenda"),
    _cal_rec("EV2", "2026:6:24:9:0:0", "2026:6:24:9:30:0", "", "", "", ""),  # no subject -> dropped
  ])
  monkeypatch.setattr(oa, "_run_osascript", lambda _s: out)
  items = list(OutlookCalendarAdapter().extract(datetime(2020, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  it = items[0]
  assert it.subject == "Standup"
  assert it.source == "outlook_calendar"
  assert it.source_id == "EV1:2026:6:23:9:0:0"
  assert "Location: Room 4" in it.content
  assert it.sender == "boss@x.io"


def test_mail_extract_and_newsletter_skip(monkeypatch):
  out = RS.join([
    _mail_rec("M1", "2026:6:23:8:0:0", "Re: budget", "alice@corp.io", "Inbox", "let's talk numbers"),
    _mail_rec("M2", "2026:6:23:8:5:0", "Sale!", "noreply@shop.io", "Inbox", "buy now"),
    _mail_rec("M3", "2026:6:23:8:6:0", "Empty", "bob@corp.io", "Inbox", "   "),  # empty body -> dropped
  ])
  monkeypatch.setattr(oa, "_run_osascript", lambda _s: out)
  adapter = OutlookMailAdapter(skip_newsletters=True)
  items = list(adapter.extract(datetime(2020, 1, 1, tzinfo=UTC)))
  ids = {it.source_id for it in items}
  assert ids == {"M1"}  # M2 newsletter, M3 empty
  assert adapter.skipped_newsletters == 1
  # Watermark advances past everything scanned, including the skipped ones.
  assert adapter.scanned_through == datetime(2026, 6, 23, 8, 6, 0).astimezone(UTC)
