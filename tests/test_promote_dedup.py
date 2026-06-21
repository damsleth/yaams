"""Tests for yaams/promote/dedup.py -- semantic dedup at promotion time.

All tests mock `yaams.promote.dedup.subprocess.run`; no real `ledger` binary,
no network, no model is required. Degrade-open contract: any subprocess failure
produces a "new" verdict so generate_candidates always proceeds.

The JSON payloads here mirror the frozen contract shape from Plan 38 Step 1.2:
a top-level `available` flag plus a `results` list whose items carry `rel_path`
and `cosine_similarity` (no `title`, no `score`).
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from yaams.promote.dedup import (
  DedupChecker,
  DedupConfig,
  check_candidate,
)

# ---------------------------------------------------------------------------
# Helpers
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


def _mock_run(stdout: str, returncode: int = 0, stderr: str = ""):
  r = MagicMock()
  r.returncode = returncode
  r.stdout = stdout
  r.stderr = stderr
  return r


def _result(rel_path: str, cosine_similarity: float) -> dict:
  return {
    "rel_path": rel_path,
    "type": "fact",
    "scope": "work",
    "status": "active",
    "lang": "en",
    "updated": "2026-05-12T00:00:00Z",
    "cosine_similarity": cosine_similarity,
  }


def _payload(available: bool, results: list[dict], reason: str = "") -> str:
  return json.dumps(
    {
      "target": "ledger",
      "backend": "local",
      "model": "BAAI/bge-m3",
      "available": available,
      "reason": reason,
      "index_built_at": "2026-06-09T10:00:00Z",
      "index_item_count": len(results),
      "results": results,
    }
  )


# ---------------------------------------------------------------------------
# check_candidate unit tests
# ---------------------------------------------------------------------------


def test_check_candidate_returns_new_below_merge_threshold(monkeypatch):
  cfg = _cfg()
  payload = _payload(True, [_result("02_facts/foo.md", 0.55)])
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run(payload),
  )
  v = check_candidate("Some statement about something", cfg)
  assert v.decision == "new"
  assert v.similarity == pytest.approx(0.55)
  assert v.target_path is None


def test_check_candidate_returns_merge_in_band(monkeypatch):
  cfg = _cfg()
  payload = _payload(True, [_result("02_facts/bar.md", 0.84)])
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run(payload),
  )
  v = check_candidate("Some statement", cfg)
  assert v.decision == "merge"
  assert v.target_path == "02_facts/bar.md"
  assert v.similarity == pytest.approx(0.84)


def test_check_candidate_returns_duplicate_above_threshold(monkeypatch):
  cfg = _cfg()
  payload = _payload(True, [_result("02_facts/dup.md", 0.94)])
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run(payload),
  )
  v = check_candidate("Exact duplicate statement", cfg)
  assert v.decision == "duplicate"
  assert v.target_path == "02_facts/dup.md"
  assert v.similarity == pytest.approx(0.94)


def test_check_candidate_handles_ledger_missing(monkeypatch):
  cfg = _cfg()

  def _raise(*a, **k):
    raise FileNotFoundError("no ledger binary")

  monkeypatch.setattr("yaams.promote.dedup.subprocess.run", _raise)
  v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert v.reason.startswith("dedup unavailable")


def test_check_candidate_handles_index_missing(monkeypatch):
  cfg = _cfg()
  payload = _payload(False, [], reason="missing_index")
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run(payload),
  )
  v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert "missing_index" in v.reason


def test_check_candidate_handles_nonzero_exit(monkeypatch):
  cfg = _cfg()
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run("", returncode=2, stderr="boom on the ledger side"),
  )
  v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert "boom on the ledger side" in v.reason


def test_check_candidate_handles_timeout(monkeypatch):
  cfg = _cfg()

  def _raise(*a, **k):
    raise subprocess.TimeoutExpired(cmd="ledger", timeout=cfg.timeout_s)

  monkeypatch.setattr("yaams.promote.dedup.subprocess.run", _raise)
  v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert v.reason.startswith("dedup unavailable")


def test_check_candidate_handles_bad_json(monkeypatch):
  cfg = _cfg()
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run("not json"),
  )
  v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert v.reason.startswith("dedup unavailable")


def test_check_candidate_disabled_returns_new(monkeypatch):
  cfg = _cfg(enabled=False)
  called = False

  def _spy(*a, **k):
    nonlocal called
    called = True
    return _mock_run(_payload(True, []))

  monkeypatch.setattr("yaams.promote.dedup.subprocess.run", _spy)
  v = check_candidate("Any statement", cfg)
  assert v.decision == "new"
  assert called is False


# ---------------------------------------------------------------------------
# DedupChecker cache tests
# ---------------------------------------------------------------------------


def test_dedup_checker_caches_by_normalized_statement(monkeypatch):
  cfg = _cfg()
  payload = _payload(True, [_result("02_facts/x.md", 0.55)])
  call_count = 0

  def _spy(*a, **k):
    nonlocal call_count
    call_count += 1
    return _mock_run(payload)

  monkeypatch.setattr("yaams.promote.dedup.subprocess.run", _spy)
  checker = DedupChecker(cfg)
  v1 = checker.check("Same statement")
  v2 = checker.check("  same   statement  ")  # whitespace + case variant

  assert v1.decision == v2.decision
  assert call_count == 1
