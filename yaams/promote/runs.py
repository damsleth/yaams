"""Run-level provenance and the candidate state machine (plan PR 3).

A promotion run is the unit of audit: which snapshot, which proposer, which
item window, which candidates. Candidates carry the run id plus a `stage` that
moves through a fixed state machine; re-running a completed stage is a no-op so
an interrupted run can be resumed without duplicating work.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

BUNDLE_SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1

# Happy path from the plan; `needs_review` and `failed` are reachable from any
# stage and can be resumed back into the flow.
_FLOW = [
  "drafted",
  "mechanically_validated",
  "llm_validated",
  "shadow_passed",
  "inbox_written",
]
_TERMINAL = {"human_accepted", "human_rejected", "merged"}
_OFF_FLOW = {"needs_review", "failed"}
STAGES = tuple(_FLOW) + tuple(sorted(_TERMINAL)) + tuple(sorted(_OFF_FLOW))


def _allowed_next(stage: str) -> set[str]:
  if stage in _OFF_FLOW:
    return set(_FLOW) | _TERMINAL | _OFF_FLOW
  if stage in _TERMINAL:
    return _OFF_FLOW
  idx = _FLOW.index(stage)
  nxt: set[str] = set(_OFF_FLOW)
  if idx + 1 < len(_FLOW):
    nxt.add(_FLOW[idx + 1])
  else:
    nxt |= _TERMINAL
  return nxt


def new_run_id() -> str:
  return f"run_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def hash_payload(payload: Any) -> str:
  return sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def start_run(
  conn: sqlite3.Connection,
  *,
  run_id: str | None = None,
  snapshot_id: str | None = None,
  backend: str = "",
  model: str | None = None,
  prompt_version: str | None = None,
  prompt_hash: str | None = None,
  temperature: float | None = None,
  seed: int | None = None,
  config_hash: str | None = None,
  parent_run_id: str | None = None,
) -> str:
  rid = run_id or new_run_id()
  conn.execute(
    """
    INSERT OR IGNORE INTO promotion_runs
      (run_id, created_at, snapshot_id, backend, model, prompt_version,
       prompt_hash, temperature, seed, config_hash, status, parent_run_id)
    VALUES (?,?,?,?,?,?,?,?,?,?,'running',?)
    """,
    (
      rid, datetime.now(UTC).isoformat(), snapshot_id, backend, model,
      prompt_version, prompt_hash, temperature, seed, config_hash, parent_run_id,
    ),
  )
  conn.commit()
  return rid


def finish_run(
  conn: sqlite3.Connection,
  run_id: str,
  *,
  status: str = "completed",
  item_ids: list[str] | None = None,
  candidate_count: int | None = None,
  duration_ms: float | None = None,
  tokens: int | None = None,
  cost: float | None = None,
  snapshot_id: str | None = None,
) -> None:
  """Close a run and pin the item window it actually drew evidence from.

  The window is the observed (ingested_at, id) bounds of the cited items, not
  the config's day window: the selector still filters on `items.timestamp`, so
  only the realized set is trustworthy. ponytail: cursor-driven selection is
  Phase 8 (`--since-last-run`); this records what happened, it does not steer it.
  """
  start = end = item_hash = None
  if item_ids:
    rows = conn.execute(
      "SELECT ingested_at, id FROM items WHERE id IN (%s) ORDER BY ingested_at, id"
      % ",".join("?" * len(item_ids)),
      list(item_ids),
    ).fetchall()
    if rows:
      start = f"{rows[0][0]}|{rows[0][1]}"
      end = f"{rows[-1][0]}|{rows[-1][1]}"
    item_hash = hash_payload(sorted(item_ids))
  conn.execute(
    """
    UPDATE promotion_runs
    SET status = ?, selection_start = COALESCE(?, selection_start),
        selection_end = COALESCE(?, selection_end),
        item_set_hash = COALESCE(?, item_set_hash),
        candidate_count = COALESCE(?, candidate_count),
        duration_ms = COALESCE(?, duration_ms),
        tokens = COALESCE(?, tokens), cost = COALESCE(?, cost),
        snapshot_id = COALESCE(?, snapshot_id)
    WHERE run_id = ?
    """,
    (status, start, end, item_hash, candidate_count, duration_ms, tokens, cost,
     snapshot_id, run_id),
  )
  conn.commit()


def advance(
  conn: sqlite3.Connection,
  candidate_id: str,
  to_stage: str,
  *,
  reason: str | None = None,
) -> bool:
  """Move a candidate to `to_stage`. Returns False when it is already there
  (idempotent resume), raises ValueError on an illegal transition."""
  if to_stage not in STAGES:
    raise ValueError(f"unknown stage: {to_stage}")
  row = conn.execute(
    "SELECT stage FROM promotion_candidates WHERE id = ?", (candidate_id,)
  ).fetchone()
  if row is None:
    raise ValueError(f"unknown candidate: {candidate_id}")
  current = row[0] or "drafted"
  if current == to_stage:
    return False
  if to_stage not in _allowed_next(current):
    raise ValueError(f"illegal transition {current} -> {to_stage}")
  conn.execute(
    "UPDATE promotion_candidates SET stage = ?, stage_reason = ? WHERE id = ?",
    (to_stage, reason, candidate_id),
  )
  conn.commit()
  return True


def _json_or_none(raw: Any) -> Any:
  if not raw:
    return None
  try:
    return json.loads(raw)
  except (TypeError, ValueError):
    return None


def _candidate_dict(row: dict[str, Any]) -> dict[str, Any]:
  action = row.get("proposed_action") or ("MERGE" if row.get("merge_with") else "ADD")
  target = row.get("target_path") or row.get("merge_with")
  out: dict[str, Any] = {
    "candidate_id": row["id"],
    "proposed_action": action,
    "target_path": target,
    "target_statement_hash": (
      row.get("target_statement_hash") or row.get("conflict_target_statement_hash")
    ),
    "note": {
      "type": row.get("draft_type") or "fact",
      "title": row["draft_title"],
      "statement": row["draft_statement"],
      "body": row.get("draft_body"),
      "tags": _json_or_none(row.get("draft_tags")) or [],
    },
    "source_item_ids": _json_or_none(row.get("source_item_ids")) or [],
    "gate_status": row.get("gate_status") or "pending",
    "human_disposition": row.get("status"),
  }
  for key, col in (
    ("evidence_map", "evidence_map"),
    ("mechanical", "mechanical"),
    ("judge", "judge"),
  ):
    val = _json_or_none(row.get(col))
    if val is not None:
      out[key] = val
  if row.get("generator_confidence") is not None:
    out["generator_confidence"] = row["generator_confidence"]
  return out


def export_bundle(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
  """Build the contract-v1 candidate bundle for one run."""
  run = conn.execute(
    "SELECT * FROM promotion_runs WHERE run_id = ?", (run_id,)
  ).fetchone()
  if run is None:
    raise ValueError(f"unknown run: {run_id}")
  run = dict(run)
  rows = [
    dict(r)
    for r in conn.execute(
      "SELECT * FROM promotion_candidates WHERE run_id = ? ORDER BY id", (run_id,)
    )
  ]
  return {
    "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
    "run_id": run_id,
    # snapshot_id is required and non-empty by the contract; on a live (unfrozen)
    # DB the run's realized item-set hash is the best available identity.
    "snapshot_id": run.get("snapshot_id") or run.get("item_set_hash") or run_id,
    "generated_at": datetime.now(UTC).isoformat(),
    "generator": {
      "backend": run.get("backend") or "",
      "model": run.get("model") or "",
      "prompt_version": run.get("prompt_version") or "",
      "temperature": run.get("temperature"),
      "seed": run.get("seed"),
    },
    "selection": {
      "selection_start": run.get("selection_start"),
      "selection_end": run.get("selection_end"),
    },
    "candidates": [_candidate_dict(r) for r in rows],
  }
