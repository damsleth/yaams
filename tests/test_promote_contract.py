"""YAAMS⇄cogled interface contract (v1), YAAMS-write side.

Pins the inbox-candidate frontmatter and inbox-path resolution that cogled's
Phase A rejection-feedback loop depends on. The contract is documented in
cognitive-ledger/docs/yaams-cogled-interface.md.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from yaams.cli import promote as promote_cli
from yaams.db import open_db
from yaams.ingest.base import Item, hash_id
from yaams.promote.review import (
  CONTRACT_VERSION,
  enrich_candidate_event_time,
  format_note,
)
from yaams.schema import init_schema
from yaams.store import store_items


def _candidate() -> dict:
  # Shape as it comes off the promotion_candidates DB row: tags and
  # source_item_ids are JSON strings, not lists.
  return {
    "id": "a1b2c3d4e5f60718",
    "entity": "AFØR",
    "draft_type": "fact",
    "draft_title": "AFØR krever minimum tre år å oppnå",
    "draft_statement": "Kvalifikasjonen tar minimum tre år.",
    "draft_body": "## Statement\nKvalifikasjonen tar minimum tre år.",
    "draft_tags": json.dumps(["røde-kors", "brkh"]),
    "source_item_ids": json.dumps(["9f2e1c0a7b3d4e5f", "6a8d2f1e0c9b3a7d"]),
  }


def test_format_note_emits_contract_v1_provenance():
  note = format_note(_candidate())

  # source must be a valid cogled enum value, not "yaams"
  assert "source: inferred\n" in note
  assert "source: yaams\n" not in note

  assert f"contract_version: {CONTRACT_VERSION}\n" in note
  assert "promoted_by: yaams\n" in note
  assert "yaams_candidate_id: a1b2c3d4e5f60718\n" in note
  # entity is JSON-quoted so non-ascii / special chars stay valid YAML
  assert 'yaams_entity: "AFØR"\n' in note
  assert "yaams_source_item_ids:\n  - 9f2e1c0a7b3d4e5f\n  - 6a8d2f1e0c9b3a7d\n" in note

  # body + sources footer preserved
  assert "# AFØR krever minimum tre år å oppnå" in note
  assert "- yaams:tier1 (promoted" in note


def test_format_note_handles_empty_source_items():
  c = _candidate()
  c["source_item_ids"] = "[]"
  c["id"] = ""
  note = format_note(c)
  assert "yaams_source_item_ids: []\n" in note
  # missing id still emits the key (degrades to item-id / entity match in cogled)
  assert "yaams_candidate_id: \n" in note


# --- Event-time bitemporal bridge (plan 04) -------------------------------


def test_format_note_omits_validity_by_default():
  # No valid_from keys on the candidate -> no validity frontmatter (v1 notes
  # stay clean, ledger treats them as valid-for-all-time).
  assert "valid_from" not in format_note(_candidate())


def test_format_note_emits_valid_from_when_present():
  c = _candidate()
  c["valid_from"] = "2025-03-01T12:00:00Z"
  note = format_note(c)
  assert "valid_from: 2025-03-01T12:00:00Z\n" in note
  assert "valid_from_confidence" not in note


def test_format_note_flags_low_confidence_when_inferred():
  c = _candidate()
  c["valid_from_confidence"] = "low"
  note = format_note(c)
  assert "valid_from_confidence: low\n" in note
  assert "valid_from:" not in note


def _store_item(conn, sid, ts, inferred=False):
  item = Item(
    id=hash_id("imessage", sid), source="imessage", source_id=sid,
    timestamp=ts, sender="a@x", recipients=["b@x"], content="c",
    timestamp_inferred=inferred,
  )
  store_items(conn, [item], [[0.1]], [[]])
  return item.id


def test_enrich_sets_valid_from_to_earliest_source_event_time(tmp_path):
  conn = open_db(tmp_path / "y.db")
  init_schema(conn, use_vec=False)
  id1 = _store_item(conn, "m1", datetime(2025, 3, 1, 12, tzinfo=UTC))
  id2 = _store_item(conn, "m2", datetime(2025, 1, 15, 9, tzinfo=UTC))
  c = {"source_item_ids": json.dumps([id1, id2])}
  enrich_candidate_event_time(conn, c)
  assert c["valid_from"] == "2025-01-15T09:00:00Z"
  assert "valid_from_confidence" not in c


def test_enrich_flags_low_confidence_when_any_source_inferred(tmp_path):
  conn = open_db(tmp_path / "y.db")
  init_schema(conn, use_vec=False)
  id1 = _store_item(conn, "m1", datetime(2025, 3, 1, 12, tzinfo=UTC), inferred=True)
  c = {"source_item_ids": json.dumps([id1])}
  enrich_candidate_event_time(conn, c)
  assert "valid_from" not in c
  assert c["valid_from_confidence"] == "low"


def test_resolve_inbox_path_explicit_config_wins(tmp_path, monkeypatch):
  # explicit config must win and short-circuit the ledger lookup
  monkeypatch.setattr(promote_cli, "_ledger_notes_dir", lambda: Path("/should/not/be/used"))
  resolved = promote_cli._resolve_inbox_path({"inbox_path": str(tmp_path / "custom")})
  assert resolved == (tmp_path / "custom").resolve()


def test_resolve_inbox_path_derives_cogled_inbox(tmp_path, monkeypatch):
  notes_dir = tmp_path / "ledger"
  notes_dir.mkdir()
  monkeypatch.setattr(promote_cli, "_ledger_notes_dir", lambda: notes_dir)
  resolved = promote_cli._resolve_inbox_path({})
  assert resolved == notes_dir / "00_inbox"


def test_resolve_inbox_path_falls_back_when_ledger_absent(monkeypatch):
  # cogled not installed → degrade open to legacy staging dir
  monkeypatch.setattr(promote_cli, "_ledger_notes_dir", lambda: None)
  resolved = promote_cli._resolve_inbox_path({})
  assert resolved == Path("~/yaams/ledger-inbox").expanduser().resolve()
