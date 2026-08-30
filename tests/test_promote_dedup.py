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
  batch_supported,
  check_batch,
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


# ---------------------------------------------------------------------------
# Batch path (`ledger embed search --batch`): probe, one-call resolution,
# line-order mapping, degrade-open, per-statement fallback
# ---------------------------------------------------------------------------

_HELP_WITH_BATCH = "Usage: ledger embed search [OPTIONS]\n  --batch  read JSONL queries on stdin\n"
_HELP_WITHOUT_BATCH = "Usage: ledger embed search [OPTIONS]\n  --query TEXT\n"


def _dispatching_run(help_stdout, batch_stdout, single_payload, calls):
  """Fake subprocess.run keyed on argv: --help probe, --batch call, single."""

  def _fake(argv, **kwargs):
    calls.append(list(argv))
    if "--help" in argv:
      return _mock_run(help_stdout)
    if "--batch" in argv:
      calls[-1] = list(argv) + [kwargs.get("input", "")]
      return _mock_run(batch_stdout)
    return _mock_run(single_payload)

  return _fake


def test_batch_supported_probes_help(monkeypatch):
  cfg = _cfg()
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run(_HELP_WITH_BATCH),
  )
  assert batch_supported(cfg) is True
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run(_HELP_WITHOUT_BATCH),
  )
  assert batch_supported(cfg) is False


def test_batch_supported_degrades_on_error(monkeypatch):
  cfg = _cfg()

  def _raise(*a, **k):
    raise FileNotFoundError("no ledger binary")

  monkeypatch.setattr("yaams.promote.dedup.subprocess.run", _raise)
  assert batch_supported(cfg) is False


def test_check_batch_maps_results_by_line_order(monkeypatch):
  cfg = _cfg()
  lines = [
    _payload(True, [_result("02_facts/a.md", 0.94)]),
    _payload(True, [_result("02_facts/b.md", 0.84)]),
    _payload(True, [_result("02_facts/c.md", 0.10)]),
  ]
  captured = {}

  def _fake(argv, **kwargs):
    captured["argv"] = list(argv)
    captured["stdin"] = kwargs.get("input", "")
    return _mock_run("\n".join(lines) + "\n")

  monkeypatch.setattr("yaams.promote.dedup.subprocess.run", _fake)
  verdicts = check_batch(["first", "second", "third"], cfg)

  assert "--batch" in captured["argv"]
  stdin_lines = [json.loads(ln) for ln in captured["stdin"].splitlines()]
  assert stdin_lines == [{"query": "first"}, {"query": "second"}, {"query": "third"}]
  assert [v.decision for v in verdicts] == ["duplicate", "merge", "new"]
  assert verdicts[0].target_path == "02_facts/a.md"
  assert verdicts[1].target_path == "02_facts/b.md"


def test_check_batch_degrades_open_on_subprocess_error(monkeypatch):
  cfg = _cfg()

  def _raise(*a, **k):
    raise subprocess.TimeoutExpired(cmd="ledger", timeout=cfg.timeout_s)

  monkeypatch.setattr("yaams.promote.dedup.subprocess.run", _raise)
  verdicts = check_batch(["one", "two"], cfg)
  assert len(verdicts) == 2
  assert all(v.decision == "new" for v in verdicts)
  assert all(v.reason.startswith("dedup unavailable") for v in verdicts)


def test_check_batch_degrades_open_on_line_count_mismatch(monkeypatch):
  cfg = _cfg()
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run(_payload(True, []) + "\n"),  # 1 line for 2 queries
  )
  verdicts = check_batch(["one", "two"], cfg)
  assert len(verdicts) == 2
  assert all(v.decision == "new" for v in verdicts)
  assert all("batch returned 1 lines for 2 queries" in v.reason for v in verdicts)


def test_check_batch_bad_line_degrades_only_that_line(monkeypatch):
  cfg = _cfg()
  lines = [_payload(True, [_result("02_facts/a.md", 0.94)]), "not json"]
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    lambda *a, **k: _mock_run("\n".join(lines) + "\n"),
  )
  verdicts = check_batch(["one", "two"], cfg)
  assert verdicts[0].decision == "duplicate"
  assert verdicts[1].decision == "new"
  assert verdicts[1].reason.startswith("dedup unavailable: json parse error")


def test_prime_uses_one_batch_call_and_fills_cache(monkeypatch, yaams_caplog):
  cfg = _cfg()
  calls: list[list] = []
  batch_stdout = "\n".join([
    _payload(True, [_result("02_facts/a.md", 0.94)]),
    _payload(True, [_result("02_facts/b.md", 0.55)]),
  ]) + "\n"
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    _dispatching_run(_HELP_WITH_BATCH, batch_stdout, _payload(True, []), calls),
  )
  checker = DedupChecker(cfg)
  # Duplicate spellings collapse to one query; empty statements never leave.
  checker.prime(["Alpha fact", "  alpha   FACT ", "Beta fact", "   "])

  search_calls = [c for c in calls if "--help" not in c]
  assert len(search_calls) == 1 and "--batch" in search_calls[0]
  stdin_lines = [json.loads(ln) for ln in search_calls[0][-1].splitlines()]
  assert [d["query"] for d in stdin_lines] == ["Alpha fact", "Beta fact"]

  # check() is now served from the cache: no further subprocess calls.
  n_calls = len(calls)
  assert checker.check("alpha fact").decision == "duplicate"
  assert checker.check("beta FACT").decision == "new"
  assert checker.check("").reason == "empty statement"
  assert len(calls) == n_calls
  assert "dedup: resolved 2 statement(s)" in yaams_caplog.text
  assert "(batch)" in yaams_caplog.text


def test_prime_falls_back_per_statement_without_batch(monkeypatch, yaams_caplog):
  cfg = _cfg()
  calls: list[list] = []
  monkeypatch.setattr(
    "yaams.promote.dedup.subprocess.run",
    _dispatching_run(
      _HELP_WITHOUT_BATCH, "", _payload(True, [_result("02_facts/x.md", 0.55)]), calls,
    ),
  )
  checker = DedupChecker(cfg)
  checker.prime(["one statement", "another statement"])

  help_calls = [c for c in calls if "--help" in c]
  search_calls = [c for c in calls if "--help" not in c]
  assert len(help_calls) == 1  # probed once per run
  assert len(search_calls) == 2
  assert all("--batch" not in c for c in search_calls)
  assert checker.check("one statement").decision == "new"
  assert len(calls) == 3  # cache hit, no new subprocess
  assert "(per-statement)" in yaams_caplog.text

  # A second prime on the same run must not re-probe --help.
  checker.prime(["a third statement"])
  assert len([c for c in calls if "--help" in c]) == 1
