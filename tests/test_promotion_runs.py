"""Run provenance, candidate state machine and contract-v1 export (plan PR 3)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from yaams.promote.candidates import PromotionCandidate, store_candidates
from yaams.promote.runs import advance, export_bundle, finish_run, start_run
from yaams.schema import init_schema

SCHEMA = json.loads(
  (Path(__file__).resolve().parent.parent / "docs" / "contracts"
   / "candidate_bundle.schema.json").read_text()
)


@pytest.fixture()
def conn():
  c = sqlite3.connect(":memory:")
  c.row_factory = sqlite3.Row
  init_schema(c, embedding_dim=8, use_vec=False)
  c.execute(
    """
    INSERT INTO items (id, source, source_id, timestamp, sender, recipients,
                       content, ingested_at)
    VALUES ('i1','chats','1','2026-01-01T00:00:00Z','kim','[]','hei','2026-02-01T00:00:00Z'),
           ('i2','chats','2','2026-01-02T00:00:00Z','kim','[]','hei igjen','2026-02-02T00:00:00Z')
    """
  )
  yield c
  c.close()


def _candidate(cid: str = "c1", **kw) -> PromotionCandidate:
  return PromotionCandidate(
    id=cid,
    entity="NOCOS",
    draft_type="fact",
    draft_title="SP betyr serviceprovider",
    draft_statement="I NOCOS betyr SP normalt serviceprovider.",
    draft_body="## Statement\nI NOCOS betyr SP normalt serviceprovider.",
    draft_tags=["abbreviation"],
    source_item_ids=["i1", "i2"],
    backend="dummy",
    **kw,
  )


def test_store_is_idempotent_within_a_run(conn):
  run_id = start_run(conn, backend="dummy", model="m")
  assert store_candidates(conn, [_candidate()], run_id=run_id) == 1
  assert store_candidates(conn, [_candidate()], run_id=run_id) == 0
  assert conn.execute("SELECT COUNT(*) FROM promotion_candidates").fetchone()[0] == 1


def test_rerun_keeps_first_owner_run_id(conn):
  first = start_run(conn, backend="dummy")
  store_candidates(conn, [_candidate()], run_id=first)
  second = start_run(conn, backend="dummy")
  assert store_candidates(conn, [_candidate()], run_id=second) == 0
  row = conn.execute("SELECT run_id, stage, gate_status, proposed_action "
                     "FROM promotion_candidates WHERE id='c1'").fetchone()
  assert row["run_id"] == first
  assert (row["stage"], row["gate_status"], row["proposed_action"]) == (
    "drafted", "pending", "ADD")


def test_finish_run_pins_the_realized_item_window(conn):
  run_id = start_run(conn)
  finish_run(conn, run_id, item_ids=["i2", "i1"], candidate_count=1)
  row = conn.execute("SELECT * FROM promotion_runs WHERE run_id=?", (run_id,)).fetchone()
  assert row["selection_start"] == "2026-02-01T00:00:00Z|i1"
  assert row["selection_end"] == "2026-02-02T00:00:00Z|i2"
  assert row["status"] == "completed"
  assert row["item_set_hash"]


def test_advance_state_machine(conn):
  run_id = start_run(conn)
  store_candidates(conn, [_candidate()], run_id=run_id)
  assert advance(conn, "c1", "mechanically_validated") is True
  # Re-running a completed stage is a no-op, so an interrupted run resumes.
  assert advance(conn, "c1", "mechanically_validated") is False
  with pytest.raises(ValueError):
    advance(conn, "c1", "inbox_written")
  assert advance(conn, "c1", "needs_review", reason="low evidence") is True
  assert advance(conn, "c1", "llm_validated") is True
  with pytest.raises(ValueError):
    advance(conn, "c1", "no_such_stage")


def test_exported_bundle_matches_contract_v1(conn):
  run_id = start_run(conn, backend="dummy", model="m", snapshot_id="abc123")
  store_candidates(
    conn,
    [_candidate(), _candidate("c2", merge_with="notes/02_facts/fact__sp.md")],
    run_id=run_id,
  )
  finish_run(conn, run_id, item_ids=["i1", "i2"], candidate_count=2)
  bundle = export_bundle(conn, run_id)
  Draft202012Validator(SCHEMA).validate(bundle)
  assert bundle["snapshot_id"] == "abc123"
  by_id = {c["candidate_id"]: c for c in bundle["candidates"]}
  assert by_id["c1"]["proposed_action"] == "ADD"
  assert by_id["c2"]["proposed_action"] == "MERGE"
  assert by_id["c2"]["target_path"] == "notes/02_facts/fact__sp.md"
  assert by_id["c1"]["source_item_ids"] == ["i1", "i2"]


def test_export_falls_back_to_item_set_hash_without_snapshot(conn):
  run_id = start_run(conn)
  store_candidates(conn, [_candidate()], run_id=run_id)
  finish_run(conn, run_id, item_ids=["i1", "i2"])
  bundle = export_bundle(conn, run_id)
  Draft202012Validator(SCHEMA).validate(bundle)
  assert bundle["snapshot_id"] == conn.execute(
    "SELECT item_set_hash FROM promotion_runs WHERE run_id=?", (run_id,)
  ).fetchone()[0]


def test_export_unknown_run_raises(conn):
  with pytest.raises(ValueError):
    export_bundle(conn, "nope")
