import json
from datetime import UTC, datetime

from yaams.ingest.base import hash_id
from yaams.ingest.ledger_notes import LedgerNotesAdapter


def _entry(rel, mtime, statement, body, **extra):
  return {"mtime": mtime, "note_type": "fact", "content_hash": "h",
          "candidate": {"rel_path": rel, "path": rel, "title": rel, "type": "fact",
                        "statement": statement, "body": body, **extra}}


def test_ledger_notes_cutoff_prepend_skip_and_stable_id(tmp_path):
  old, new = datetime(2026, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 9, 1, tzinfo=UTC).timestamp()
  index = tmp_path / "note_index.json"
  index.write_text(json.dumps({"entries": {
    "a": _entry("notes/02_facts/a.md", new, "Alpha fact statement here", "# Alpha fact statement here\n\nmore"),
    "b": _entry("notes/02_facts/b.md", new, "Beta is the fact", "# B\n\nContext body that does not open with it"),
    "c": _entry("notes/02_facts/c.md", new, "", "tiny"),
    "d": _entry("notes/02_facts/d.md", old, "Old note statement", "Old note statement body text"),
  }}))
  adapter = LedgerNotesAdapter(notes_path=tmp_path, index_path=index)
  items = {i.source_id: i for i in adapter.extract(datetime(2026, 6, 1, tzinfo=UTC))}

  assert set(items) == {"notes/02_facts/a.md", "notes/02_facts/b.md"}  # d before cutoff, c too short
  assert adapter.skipped_empty == 1
  assert items["notes/02_facts/a.md"].content.startswith("# Alpha")  # body leads with it: no prepend
  assert items["notes/02_facts/b.md"].content.startswith("Beta is the fact\n\n# B")
  a = items["notes/02_facts/a.md"]
  assert a.source == "tier2_ledger" and a.id == hash_id("tier2_ledger", "notes/02_facts/a.md")


def test_ledger_notes_statement_doubling_is_deliberate(tmp_path):
  # Real bodies open "# Title\n\n## Statement", so the statement is indexed
  # twice (extra BM25 weight). Kept on purpose: removing it lost on the harness
  # (experiments.jsonl `ledger_statement_dedup`, 2026-09-25).
  index = tmp_path / "note_index.json"
  body = "# Title\n\n## Statement\nGamma statement text"
  index.write_text(json.dumps({"entries": {"g": _entry("n/g.md", 2e9, "Gamma statement text", body)}}))
  (item,) = LedgerNotesAdapter(notes_path=tmp_path, index_path=index).extract(datetime(2026, 1, 1, tzinfo=UTC))
  assert item.content == "Gamma statement text\n\n" + body
