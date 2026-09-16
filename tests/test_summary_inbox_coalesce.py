"""Same-day ingest digests coalesce into one 00_inbox note.

Three runs on 2026-08-20 wrote three near-identical digests that cogled's
`sleep duplicates` flagged at 0.75 title-jaccard against each other.
"""
from __future__ import annotations

from datetime import UTC, datetime

from yaams.synthesize.summarize import write_summary_to_inbox
from yaams.time import to_local


def _write(monkeypatch, tmp_path, text, when):
  monkeypatch.setattr(
    "yaams.synthesize.summarize._ledger_inbox_dir", lambda: str(tmp_path)
  )
  return write_summary_to_inbox(text, when=when)


# Section headers and the day boundary follow the owner's local clock, so the
# expectations are derived rather than hardcoded — otherwise this suite only
# passes in UTC. Frontmatter stays UTC and *is* hardcoded.
_FIRST = datetime(2026, 8, 20, 10, 12, tzinfo=UTC)
_SECOND = datetime(2026, 8, 20, 10, 55, tzinfo=UTC)


def test_same_day_runs_append_to_one_note(tmp_path, monkeypatch):
  _write(monkeypatch, tmp_path, "first digest", _FIRST)
  _write(monkeypatch, tmp_path, "second digest", _SECOND)

  notes = sorted((tmp_path / "00_inbox").glob("note__ingest_summary_*.md"))
  assert len(notes) == 1, f"expected one note for the day, got {[n.name for n in notes]}"

  body = notes[0].read_text(encoding="utf-8")
  assert body.count("created: ") == 1
  assert "created: 2026-08-20T10:12:00Z" in body   # first run, UTC
  assert "updated: 2026-08-20T10:55:00Z" in body   # bumped by the second, UTC
  assert f"## {to_local(_FIRST):%H:%M}\n\nfirst digest" in body
  assert f"## {to_local(_SECOND):%H:%M}\n\nsecond digest" in body


def test_next_day_starts_a_new_note(tmp_path, monkeypatch):
  # Midday UTC, so these land on different local days in any plausible tz.
  mon = datetime(2026, 8, 20, 12, tzinfo=UTC)
  tue = datetime(2026, 8, 21, 12, tzinfo=UTC)
  _write(monkeypatch, tmp_path, "mon", mon)
  _write(monkeypatch, tmp_path, "tue", tue)
  notes = sorted((tmp_path / "00_inbox").glob("note__ingest_summary_*.md"))
  assert [n.name for n in notes] == [
    f"note__ingest_summary_{to_local(mon):%Y_%m_%d}.md",
    f"note__ingest_summary_{to_local(tue):%Y_%m_%d}.md",
  ]
