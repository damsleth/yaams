"""Semantic dedup check for promotion candidates.

Calls `ledger embed search --target ledger --query <statement> --limit 1 --json`
as a subprocess and interprets the response to decide whether a drafted
candidate is new, should be merged into an existing note, or is a duplicate.

Batch mode: when the installed ledger CLI supports `--batch` (probed via
`ledger embed search --help`, once per run), all uncached statements for a
promote run are resolved in ONE subprocess call - JSONL queries on stdin
(`{"query": ...}` per line), JSONL results on stdout mapped back by line
order, each line the same payload shape as the single-query response. One
call means one warm encoder instead of a cold model load per statement.
Callers hand the run's statements to ``DedupChecker.prime`` up front;
``check`` then serves from the cache. When `--batch` is unsupported, prime
falls back to the per-statement path unchanged. Both paths log the dedup
phase's wall time so the speedup is measurable.

Degrade-open contract: any subprocess error (ledger not installed, index
missing, timeout) returns verdict("new", ...) with reason="dedup unavailable:
<exc>" so the caller always proceeds without blocking.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)


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


def _unavailable(reason: str) -> DedupVerdict:
  return DedupVerdict("new", None, 0.0, f"dedup unavailable: {reason}")


def _interpret(payload: dict, config: DedupConfig) -> DedupVerdict:
  """Map one `ledger embed search` JSON payload to a verdict.

  Shared by the single-query and batch paths: a batch stdout line carries the
  same shape as the single-query response.
  """
  # Graceful degradation: index not built yet.
  if not payload.get("available", True):
    reason = payload.get("reason", "missing_index")
    return _unavailable(reason)

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
    return _unavailable(str(exc))

  if result.returncode != 0:
    stderr = result.stderr.strip()
    return _unavailable(f"exit {result.returncode}: {stderr[:120]}")

  try:
    payload = json.loads(result.stdout)
  except Exception as exc:
    return _unavailable(f"json parse error: {exc}")

  return _interpret(payload, config)


def batch_supported(config: DedupConfig) -> bool:
  """Probe `ledger embed search --help` for `--batch`. Never raises."""
  try:
    result = subprocess.run(
      [config.ledger_cli, "embed", "search", "--help"],
      capture_output=True,
      text=True,
      timeout=config.timeout_s,
    )
  except Exception:
    return False
  return result.returncode == 0 and "--batch" in (result.stdout or "")


def check_batch(statements: list[str], config: DedupConfig) -> list[DedupVerdict]:
  """Resolve every statement in one `ledger embed search --batch` call.

  Statements must be non-empty (callers strip and filter first). Results map
  back by line order. Degrade-open: a failed call yields "new" for every
  statement; a malformed stdout line degrades only that line.
  """
  if not statements:
    return []

  stdin_payload = "".join(json.dumps({"query": s}) + "\n" for s in statements)
  # The per-statement timeout covers a cold encoder load; the batch pays that
  # once, then each additional query is a warm encode. 2s each is generous.
  timeout_s = config.timeout_s + 2 * (len(statements) - 1)
  try:
    result = subprocess.run(
      [
        config.ledger_cli,
        "embed",
        "search",
        "--target", "ledger",
        "--limit", "1",
        "--json",
        "--batch",
      ],
      input=stdin_payload,
      capture_output=True,
      text=True,
      timeout=timeout_s,
    )
  except Exception as exc:
    return [_unavailable(str(exc)) for _ in statements]

  if result.returncode != 0:
    stderr = result.stderr.strip()
    return [
      _unavailable(f"exit {result.returncode}: {stderr[:120]}")
      for _ in statements
    ]

  lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
  if len(lines) != len(statements):
    reason = f"batch returned {len(lines)} lines for {len(statements)} queries"
    return [_unavailable(reason) for _ in statements]

  verdicts: list[DedupVerdict] = []
  for line in lines:
    try:
      payload = json.loads(line)
    except Exception as exc:
      verdicts.append(_unavailable(f"json parse error: {exc}"))
      continue
    verdicts.append(_interpret(payload, config))
  return verdicts


class DedupChecker:
  """Caches verdicts per normalized statement within a promote run.

  YAAMS clusters often produce near-identical statements across entities;
  caching avoids redundant subprocess calls. ``prime`` resolves a whole run's
  statements up front (one batch call when the CLI supports it); ``check``
  then serves from the cache and remains the degrade path for anything primed
  after the fact.
  """

  def __init__(self, config: DedupConfig) -> None:
    self.config = config
    self._cache: dict[str, DedupVerdict] = {}
    self._batch_supported: bool | None = None  # probed once per run

  @staticmethod
  def _key(statement: str) -> str:
    return " ".join(statement.lower().split())

  def check(self, statement: str) -> DedupVerdict:
    key = self._key(statement)
    if key not in self._cache:
      self._cache[key] = check_candidate(statement, self.config)
    return self._cache[key]

  def prime(self, statements: list[str]) -> None:
    """Resolve verdicts for every uncached statement, batched when possible.

    Logs the dedup phase's wall time in both the batch and the per-statement
    fallback path.
    """
    if not self.config.enabled:
      return
    pending: dict[str, str] = {}
    for statement in statements:
      key = self._key(statement)
      if key in self._cache or key in pending:
        continue
      stripped = statement.strip()
      if not stripped:
        self._cache[key] = DedupVerdict("new", None, 0.0, "empty statement")
        continue
      pending[key] = stripped
    if not pending:
      return

    t0 = time.perf_counter()
    if self._batch_supported is None:
      self._batch_supported = batch_supported(self.config)
    if self._batch_supported:
      mode = "batch"
      verdicts = check_batch(list(pending.values()), self.config)
      for key, verdict in zip(pending, verdicts):
        self._cache[key] = verdict
    else:
      mode = "per-statement"
      for key, statement in pending.items():
        self._cache[key] = check_candidate(statement, self.config)
    log.info(
      "dedup: resolved %d statement(s) in %.2fs (%s)",
      len(pending), time.perf_counter() - t0, mode,
    )
