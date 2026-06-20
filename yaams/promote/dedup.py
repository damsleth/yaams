"""Semantic dedup check for promotion candidates.

Calls `ledger embed search --target ledger --query <statement> --limit 1 --json`
as a subprocess and interprets the response to decide whether a drafted
candidate is new, should be merged into an existing note, or is a duplicate.

Degrade-open contract: any subprocess error (ledger not installed, index
missing, timeout) returns verdict("new", ...) with reason="dedup unavailable:
<exc>" so the caller always proceeds without blocking.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Literal


@dataclass
class DedupVerdict:
  decision: Literal["new", "merge", "duplicate"]
  target_path: str | None
  similarity: float
  reason: str


@dataclass
class DedupConfig:
  enabled: bool = True
  duplicate_threshold: float = 0.92
  merge_threshold: float = 0.80
  embed_backend: str = "local"
  ledger_cli: str = "ledger"
  timeout_s: int = 15


def check_candidate(statement: str, config: DedupConfig) -> DedupVerdict:
  """Run a single dedup check against the ledger embed index.

  Returns a DedupVerdict. Never raises -- any error produces a "new" verdict
  with reason starting "dedup unavailable:".
  """
  if not config.enabled:
    return DedupVerdict("new", None, 0.0, "dedup disabled")

  statement = statement.strip()
  if not statement:
    return DedupVerdict("new", None, 0.0, "empty statement")

  try:
    result = subprocess.run(
      [
        config.ledger_cli,
        "embed",
        "search",
        "--target", "ledger",
        "--query", statement,
        "--limit", "1",
        "--json",
      ],
      capture_output=True,
      text=True,
      timeout=config.timeout_s,
    )
  except Exception as exc:
    return DedupVerdict("new", None, 0.0, f"dedup unavailable: {exc}")

  if result.returncode != 0:
    stderr = result.stderr.strip()
    return DedupVerdict("new", None, 0.0, f"dedup unavailable: exit {result.returncode}: {stderr[:120]}")

  try:
    payload = json.loads(result.stdout)
  except Exception as exc:
    return DedupVerdict("new", None, 0.0, f"dedup unavailable: json parse error: {exc}")

  # Graceful degradation: index not built yet.
  if not payload.get("available", True):
    reason = payload.get("reason", "missing_index")
    return DedupVerdict("new", None, 0.0, f"dedup unavailable: {reason}")

  results = payload.get("results") or []
  if not results:
    return DedupVerdict("new", None, 0.0, "sim=0.00")

  top = results[0]
  sim = float(top.get("cosine_similarity", 0.0))
  rel_path = top.get("rel_path") or None

  if sim >= config.duplicate_threshold:
    return DedupVerdict("duplicate", rel_path, sim, f"sim={sim:.2f}")
  if sim >= config.merge_threshold:
    return DedupVerdict("merge", rel_path, sim, f"sim={sim:.2f}")
  return DedupVerdict("new", None, sim, f"sim={sim:.2f}")


class DedupChecker:
  """Caches verdicts per normalized statement within a promote run.

  YAAMS clusters often produce near-identical statements across entities;
  caching avoids redundant subprocess calls.
  """

  def __init__(self, config: DedupConfig) -> None:
    self.config = config
    self._cache: dict[str, DedupVerdict] = {}

  def check(self, statement: str) -> DedupVerdict:
    key = " ".join(statement.lower().split())
    if key not in self._cache:
      self._cache[key] = check_candidate(statement, self.config)
    return self._cache[key]
