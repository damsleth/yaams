"""Tests for yaams/promote/dedup.py -- semantic dedup at promotion time.

All tests use subprocess.run mocks; no real `ledger` binary, no network, no
model is required. Degrade-open contract: any subprocess failure produces a
"new" verdict so generate_candidates always proceeds.
"""
from __future__ import annotations

import json
import sqlite3
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from yaams.promote.candidates import (
  PromoteConfig,
  _candidate_id,
  generate_candidates,
)
from yaams.promote.dedup import (
  DedupChecker,
  DedupConfig,
  DedupVerdict,
  check_candidate,
)
from yaams.schema import init_schema
from yaams.store import store_items
from yaams.synthesize.llm import LLMResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _add_entity(conn, name: str, etype: str = "org") -> int:
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
    (name, etype),
  )
  conn.commit()
  return conn.execute(
    "SELECT id FROM entities WHERE canonical_name = ?", (name,)
  ).fetchone()["id"]


def _add_item(conn, key: str, entity_id: int) -> str:
  from yaams.ingest.base import Item, hash_id
  item = Item(
    id=hash_id("imessage", f"t:{key}"),
    source="imessage",
    source_id=f"t:{key}",
    timestamp=datetime.now(UTC),
    sender="a@test",
    recipients=[],
    content=f"content about the entity {key}",
    subject="",
    thread_id="t",
  )
  store_items(conn, [item], [b"\x00" * 16], [[]])
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, ?)",
    (item.id, entity_id, "dictionary"),
  )
  conn.commit()
  return item.id


def _seed_cluster(conn, entity_name: str, n: int = 3) -> tuple[str, list[str]]:
  eid = _add_entity(conn, entity_name)
  for i in range(n):
    _add_item(conn, f"{entity_name}-{i}", eid)
  rows = conn.execute(
    """
    SELECT i.id FROM item_entities ie
    JOIN items i ON i.id = ie.item_id
    WHERE ie.entity_id = ? AND i.source NOT IN ('tier2_ledger')
    ORDER BY i.timestamp DESC
    """,
    (eid,),
  ).fetchall()
  return entity_name, [r["id"] for r in rows]


_DRAFT_YAML = """\
type: fact
title: {title}
statement: A clear statement about {entity}.
tags:
  - t1
body: |
  ## Statement
  A clear statement about {entity}.
"""


class _FakeAdapter:
  backend_name = "fake"
  model_name = "fake-model"

  def __init__(self, title: str = "A drafted title"):
    self._title = title

  def complete(self, prompt, *, max_tokens=1024, temperature=0.0):
    entity = "entity"
    for line in prompt.splitlines():
      if 'about "' in line:
        entity = line.split('about "', 1)[1].split('"', 1)[0]
        break
    return LLMResponse(
      text=_DRAFT_YAML.format(title=self._title, entity=entity),
      backend=self.backend_name,
      model=self.model_name,
    )


def _make_payload(available: bool, hits: list[dict]) -> str:
  return json.dumps({"available": available, "hits": hits})


def _hit(rel_path: str, score: float) -> dict:
  return {"rel_path": rel_path, "score": score}


# ---------------------------------------------------------------------------
# check_candidate unit tests
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> DedupConfig:
  defaults = dict(
    enabled=True,
    duplicate_threshold=0.92,
    merge_threshold=0.80,
    ledger_cli="ledger",
    timeout_s=5,
  )
  defaults.update(kwargs)
  return DedupConfig(**defaults)


def _mock_run(stdout: str, returncode: int = 0):
  r = MagicMock()
  r.returncode = returncode
  r.stdout = stdout
  r.stderr = ""
  return r


def test_check_candidate_returns_new_below_merge_threshold():
  cfg = _cfg()
  payload = _make_payload(True, [_hit("notes/foo.md", 0.55)])
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    v = check_candidate("Some statement about something", cfg)
  assert v.decision == "new"
  assert v.similarity == pytest.approx(0.55)
  assert v.reason == "sim=0.55"
  assert v.target_path is None


def test_check_candidate_returns_merge_in_band():
  cfg = _cfg()
  payload = _make_payload(True, [_hit("notes/bar.md", 0.84)])
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    v = check_candidate("Some statement", cfg)
  assert v.decision == "merge"
  assert v.target_path == "notes/bar.md"
  assert v.similarity == pytest.approx(0.84)


def test_check_candidate_returns_duplicate_above_threshold():
  cfg = _cfg()
  payload = _make_payload(True, [_hit("notes/dup.md", 0.95)])
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    v = check_candidate("Exact duplicate statement", cfg)
  assert v.decision == "duplicate"
  assert v.target_path == "notes/dup.md"
  assert v.similarity == pytest.approx(0.95)


def test_check_candidate_missing_index_returns_new():
  cfg = _cfg()
  payload = json.dumps({"available": False, "reason": "missing_index"})
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert "missing_index" in v.reason


def test_check_candidate_subprocess_exception_returns_new():
  cfg = _cfg()
  with patch("yaams.promote.dedup.subprocess.run", side_effect=FileNotFoundError("no ledger")):
    v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert "dedup unavailable" in v.reason


def test_check_candidate_nonzero_exit_returns_new():
  cfg = _cfg()
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run("", returncode=1)):
    v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert "dedup unavailable" in v.reason


def test_check_candidate_bad_json_returns_new():
  cfg = _cfg()
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run("not json {{{}")):
    v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert "dedup unavailable" in v.reason


def test_check_candidate_empty_statement_returns_new():
  cfg = _cfg()
  v = check_candidate("   ", cfg)
  assert v.decision == "new"
  assert v.reason == "empty statement"


def test_check_candidate_disabled_returns_new():
  cfg = _cfg(enabled=False)
  v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert v.reason == "dedup disabled"


def test_check_candidate_empty_hits_returns_new():
  cfg = _cfg()
  payload = _make_payload(True, [])
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    v = check_candidate("Some statement", cfg)
  assert v.decision == "new"


def test_check_candidate_uses_limit_1():
  cfg = _cfg()
  payload = _make_payload(True, [_hit("notes/x.md", 0.5)])
  captured: list[list] = []

  def fake_run(cmd, **kwargs):
    captured.append(cmd)
    return _mock_run(payload)

  with patch("yaams.promote.dedup.subprocess.run", side_effect=fake_run):
    check_candidate("test statement", cfg)

  assert "--limit" in captured[0]
  limit_idx = captured[0].index("--limit")
  assert captured[0][limit_idx + 1] == "1"


# ---------------------------------------------------------------------------
# DedupChecker cache tests
# ---------------------------------------------------------------------------


def test_dedup_checker_caches_per_normalized_statement():
  cfg = _cfg()
  payload = _make_payload(True, [_hit("notes/x.md", 0.55)])
  call_count = 0

  def fake_run(cmd, **kwargs):
    nonlocal call_count
    call_count += 1
    return _mock_run(payload)

  with patch("yaams.promote.dedup.subprocess.run", side_effect=fake_run):
    checker = DedupChecker(cfg)
    v1 = checker.check("Same statement")
    v2 = checker.check("same statement")   # normalized same
    v3 = checker.check("  Same   statement  ")  # extra spaces

  assert v1.decision == v2.decision == v3.decision
  # All three map to the same cache key; subprocess called once.
  assert call_count == 1


def test_dedup_checker_different_statements_not_cached():
  cfg = _cfg()
  payload = _make_payload(True, [_hit("notes/x.md", 0.55)])
  call_count = 0

  def fake_run(cmd, **kwargs):
    nonlocal call_count
    call_count += 1
    return _mock_run(payload)

  with patch("yaams.promote.dedup.subprocess.run", side_effect=fake_run):
    checker = DedupChecker(cfg)
    checker.check("First statement")
    checker.check("Second statement")

  assert call_count == 2


# ---------------------------------------------------------------------------
# generate_candidates integration: dedup wired from config
# ---------------------------------------------------------------------------


def test_generate_candidates_dedup_disabled_passes_through():
  """When dedup is disabled, candidates flow through regardless of sim score."""
  conn = _open_db()
  _seed_cluster(conn, "OrgA")
  cfg = PromoteConfig(
    min_cluster_items=3,
    dedup=DedupConfig(enabled=False),
  )
  # Subprocess should NOT be called when dedup disabled.
  with patch("yaams.promote.dedup.subprocess.run") as mock_run:
    result = generate_candidates(conn, _FakeAdapter(), cfg)
  mock_run.assert_not_called()
  assert len(result) == 1


def test_generate_candidates_dedup_duplicate_skips_candidate():
  """Sim >= duplicate_threshold causes candidate to be skipped."""
  conn = _open_db()
  _seed_cluster(conn, "OrgB")
  cfg = PromoteConfig(
    min_cluster_items=3,
    dedup=DedupConfig(enabled=True, duplicate_threshold=0.92, merge_threshold=0.80),
  )
  payload = _make_payload(True, [_hit("notes/dup.md", 0.95)])
  msgs: list[str] = []
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    result = generate_candidates(conn, _FakeAdapter(), cfg, on_progress=msgs.append)
  assert result == []
  assert any("dedup duplicate" in m for m in msgs)


def test_generate_candidates_dedup_merge_sets_merge_with():
  """Sim in merge band sets merge_with and dedup_similarity on candidate."""
  conn = _open_db()
  _seed_cluster(conn, "OrgC")
  cfg = PromoteConfig(
    min_cluster_items=3,
    dedup=DedupConfig(enabled=True, duplicate_threshold=0.92, merge_threshold=0.80),
  )
  payload = _make_payload(True, [_hit("notes/merge_target.md", 0.84)])
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    result = generate_candidates(conn, _FakeAdapter(), cfg)
  assert len(result) == 1
  assert result[0].merge_with == "notes/merge_target.md"
  assert result[0].dedup_similarity == pytest.approx(0.84)


def test_generate_candidates_dedup_new_appends_normally():
  """Sim below merge threshold: candidate appended with no merge_with."""
  conn = _open_db()
  _seed_cluster(conn, "OrgD")
  cfg = PromoteConfig(
    min_cluster_items=3,
    dedup=DedupConfig(enabled=True, duplicate_threshold=0.92, merge_threshold=0.80),
  )
  payload = _make_payload(True, [_hit("notes/unrelated.md", 0.50)])
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    result = generate_candidates(conn, _FakeAdapter(), cfg)
  assert len(result) == 1
  assert result[0].merge_with is None
  assert result[0].dedup_similarity is None


def test_generate_candidates_dedup_unavailable_passes_through():
  """When ledger is not found, candidate still flows through (degrade-open)."""
  conn = _open_db()
  _seed_cluster(conn, "OrgE")
  cfg = PromoteConfig(
    min_cluster_items=3,
    dedup=DedupConfig(enabled=True),
  )
  with patch(
    "yaams.promote.dedup.subprocess.run",
    side_effect=FileNotFoundError("ledger not installed"),
  ):
    result = generate_candidates(conn, _FakeAdapter(), cfg)
  assert len(result) == 1
  assert result[0].merge_with is None


def test_generate_candidates_dedup_missing_index_passes_through():
  """available:false in ledger response -> candidate still flows through."""
  conn = _open_db()
  _seed_cluster(conn, "OrgF")
  cfg = PromoteConfig(
    min_cluster_items=3,
    dedup=DedupConfig(enabled=True),
  )
  payload = json.dumps({"available": False, "reason": "missing_index"})
  with patch("yaams.promote.dedup.subprocess.run", return_value=_mock_run(payload)):
    result = generate_candidates(conn, _FakeAdapter(), cfg)
  assert len(result) == 1
