"""Notes timestamp resolution (yaams.ingest.obsidian._note_timestamp).

Priority: frontmatter date → filename date → date in title/H1 → mtime. Only the
mtime fallback is flagged inferred, so recency retrieval can exclude undated
notes that a bulk import collapsed onto one recent date.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from yaams.ingest.obsidian import _note_timestamp

_MTIME = datetime(2026, 5, 22, 9, 0, tzinfo=UTC)


def test_frontmatter_date_wins_and_is_not_inferred():
  ts, inferred = _note_timestamp(
    {"date": "2025-03-14"}, Path("whatever.md"), "# body", _MTIME
  )
  assert ts == datetime(2025, 3, 14, tzinfo=UTC)
  assert inferred is False


def test_filename_date_prefix():
  ts, inferred = _note_timestamp(
    {}, Path("2024-11-02-standup.md"), "# notes", _MTIME
  )
  assert ts == datetime(2024, 11, 2, tzinfo=UTC)
  assert inferred is False


def test_norwegian_daily_note_h1_date():
  ts, inferred = _note_timestamp(
    {}, Path("mandag.md"), "# 18.mai 2026 - mandag\n\nstuff", _MTIME
  )
  assert ts == datetime(2026, 5, 18, tzinfo=UTC)
  assert inferred is False


def test_english_daily_note_h1_date():
  ts, inferred = _note_timestamp(
    {}, Path("note.md"), "# 18.October 2024 - fredag", _MTIME
  )
  assert ts == datetime(2024, 10, 18, tzinfo=UTC)
  assert inferred is False


def test_iso_date_in_title():
  ts, inferred = _note_timestamp(
    {"title": "Standup 2025-07-09"}, Path("x.md"), "# Standup", _MTIME
  )
  assert ts == datetime(2025, 7, 9, tzinfo=UTC)
  assert inferred is False


def test_undated_note_falls_back_to_mtime_and_is_inferred():
  ts, inferred = _note_timestamp(
    {}, Path("tinnitus.md"), "# Tinnitus\n\nStartet ca 2023.", _MTIME
  )
  assert ts == _MTIME
  assert inferred is True


def test_underscore_notes_are_ingested_by_default(tmp_path):
  """The vault uses a leading "_" for per-folder hub notes (_did, _swon, _une).

  Skipping them hid ~32 of the densest notes from recall. Opt in explicitly if
  you want the old behaviour.
  """
  from datetime import datetime, timezone

  from yaams.ingest.obsidian import ObsidianAdapter

  body = "mye kontekst her, og nok tegn til aa passere MIN_CONTENT_CHARS. " * 8
  (tmp_path / "_une.md").write_text(f"# UNE hub\n\n{body}\n")
  (tmp_path / "vanlig.md").write_text(f"# Vanlig\n\n{body}\n")
  epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

  seen = {i.source_id for i in ObsidianAdapter(vault_path=tmp_path).extract(epoch)}
  assert any("_une" in s for s in seen), seen

  opted_out = ObsidianAdapter(vault_path=tmp_path, skip_filename_prefixes=("_",))
  assert not any("_une" in i.source_id for i in opted_out.extract(epoch))
