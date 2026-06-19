from __future__ import annotations

import json as _json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Version of the YAAMS⇄cogled interface contract this writer emits.
# Pinned in cognitive-ledger/docs/yaams-cogled-interface.md. Bump only on a
# breaking frontmatter field rename/removal; additive fields do not bump.
CONTRACT_VERSION = 1


def _coerce_list(raw: Any) -> list[str]:
  """Tags / item-ids arrive as a JSON string (from the DB row) or a list
  (from a fresh PromotionCandidate). Normalize either to a list[str]."""
  if isinstance(raw, str):
    try:
      val = _json.loads(raw)
    except Exception:
      val = [s.strip() for s in raw.split(",") if s.strip()]
  else:
    val = list(raw or [])
  return [str(v) for v in val]


def format_note(candidate: dict[str, Any]) -> str:
  now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
  title = candidate.get("draft_title") or "Untitled"
  statement = candidate.get("draft_statement") or ""
  body = candidate.get("draft_body") or f"## Statement\n{statement}"
  tags_list = _coerce_list(candidate.get("draft_tags"))
  tags_yaml = "\n".join(f"  - {t}" for t in tags_list) if tags_list else "  - general"

  # Provenance block — lets cogled build a stable rejection signature from a
  # file on disk (Phase A). Without these, deleting an inbox file is silent and
  # YAAMS re-proposes the same cluster next run. `source: inferred` (not
  # "yaams") keeps cogled's source enum (user|assistant|tool|inferred) closed;
  # origin is carried by `promoted_by`.
  candidate_id = str(candidate.get("id") or "")
  entity = str(candidate.get("entity") or "")
  item_ids = _coerce_list(candidate.get("source_item_ids"))
  if item_ids:
    item_ids_yaml = "yaams_source_item_ids:\n" + "\n".join(f"  - {i}" for i in item_ids)
  else:
    item_ids_yaml = "yaams_source_item_ids: []"

  # Conflict block — emitted only when conflict_classification is present
  conflict_block = ""
  conflict_classification = candidate.get("conflict_classification")
  if conflict_classification:
    merge_with = candidate.get("merge_with") or ""
    dedup_similarity = candidate.get("dedup_similarity")
    conflict_confidence = candidate.get("conflict_confidence")
    conflict_reason = candidate.get("conflict_reason") or ""
    conflict_model = candidate.get("conflict_model") or ""
    conflict_checked_at = candidate.get("conflict_checked_at") or ""
    conflict_target_statement_hash = candidate.get("conflict_target_statement_hash") or ""
    conflict_prompt_version = candidate.get("conflict_prompt_version") or ""

    lines = []
    if merge_with:
      lines.append(f"merge_with: {merge_with}")
    if dedup_similarity is not None:
      lines.append(f"dedup_similarity: {dedup_similarity:.2f}")
    lines.append(f"conflict_classification: {conflict_classification}")
    if conflict_confidence is not None:
      lines.append(f"conflict_confidence: {conflict_confidence:.2f}")
    lines.append(f"conflict_reason: {conflict_reason}")
    lines.append(f"conflict_model: {conflict_model}")
    lines.append(f"conflict_checked_at: {conflict_checked_at}")
    if conflict_target_statement_hash:
      lines.append(f"conflict_target_statement_hash: {conflict_target_statement_hash}")
    if conflict_prompt_version:
      lines.append(f"conflict_prompt_version: {conflict_prompt_version}")
    conflict_block = "\n".join(lines) + "\n"
  elif candidate.get("merge_with"):
    # Phase C dedup only (no conflict classification)
    merge_with = candidate.get("merge_with")
    dedup_similarity = candidate.get("dedup_similarity")
    lines = [f"merge_with: {merge_with}"]
    if dedup_similarity is not None:
      lines.append(f"dedup_similarity: {dedup_similarity:.2f}")
    conflict_block = "\n".join(lines) + "\n"

  return (
    f"---\n"
    f"created: {now}\n"
    f"updated: {now}\n"
    f"tags:\n{tags_yaml}\n"
    f"confidence: 0.7\n"
    f"source: inferred\n"
    f"scope: personal\n"
    f"lang: en\n"
    f"contract_version: {CONTRACT_VERSION}\n"
    f"promoted_by: yaams\n"
    f"yaams_candidate_id: {candidate_id}\n"
    f"yaams_entity: {_json.dumps(entity, ensure_ascii=False)}\n"
    f"{item_ids_yaml}\n"
    f"{conflict_block}"
    f"---\n\n"
    f"# {title}\n\n"
    f"{body.strip()}\n\n"
    f"## Sources\n"
    f"- yaams:tier1 (promoted {now[:10]})\n"
  )


def note_filename(candidate: dict[str, Any]) -> str:
  note_type = candidate.get("draft_type") or "fact"
  title = candidate.get("draft_title") or "untitled"
  slug = title.lower()
  slug = re.sub(r"[^a-z0-9\s]", "", slug)
  slug = re.sub(r"\s+", "_", slug.strip())
  slug = slug[:60]
  return f"{note_type}__{slug}.md"


def write_to_inbox(
  candidate: dict[str, Any],
  inbox_path: Path,
  content: str | None = None,
) -> Path:
  # Contradictions are routed to _conflicts/ subdirectory so cogled can load
  # them separately via `ledger inbox conflicts`.
  if candidate.get("conflict_classification") == "contradict":
    inbox_path = inbox_path / "_conflicts"

  inbox_path.mkdir(parents=True, exist_ok=True)
  filename = note_filename(candidate)
  dest = inbox_path / filename

  # avoid clobbering an existing file
  if dest.exists():
    ts = datetime.now(UTC).strftime("%H%M%S")
    dest = inbox_path / filename.replace(".md", f"_{ts}.md")

  dest.write_text(content or format_note(candidate), encoding="utf-8")
  return dest


def write_candidate_to_ledger(
  conn: sqlite3.Connection,
  candidate: dict[str, Any],
  inbox_path: Path,
) -> dict[str, Any]:
  """Write one candidate to the inbox and update the DB — shared write path
  used by both the interactive ``promote review`` TUI and the headless
  ``promote commit`` verb.

  Idempotent: if the candidate already has status='accepted' in the DB we
  return its existing promoted_path without writing a duplicate file.

  Returns a dict with keys: ``candidate_id``, ``ledger_note``, ``status``
  where status is either ``'written'`` (new write) or ``'already_accepted'``
  (no-op, idempotent).
  """
  from yaams.promote.candidates import mark_items_promoted, update_status

  cid = str(candidate.get("id") or "")

  # Idempotency check — if already accepted, return existing path
  if candidate.get("status") == "accepted":
    return {
      "candidate_id": cid,
      "ledger_note": candidate.get("promoted_path") or "",
      "status": "already_accepted",
    }

  note_content = format_note(candidate)
  dest = write_to_inbox(candidate, inbox_path, content=note_content)

  try:
    item_ids = _json.loads(candidate.get("source_item_ids") or "[]")
  except Exception:
    item_ids = []

  mark_items_promoted(conn, item_ids, str(dest))
  update_status(conn, cid, "accepted", promoted_path=str(dest))

  return {
    "candidate_id": cid,
    "ledger_note": str(dest),
    "status": "written",
  }


def render_candidate(candidate: dict[str, Any], index: int, total: int) -> str:
  tags = ", ".join(_coerce_list(candidate.get("draft_tags")))
  n_sources = len(_coerce_list(candidate.get("source_item_ids")))

  lines = [
    f"\nCandidate {index}/{total} - {candidate.get('draft_type', 'fact')} - entity: {candidate.get('entity', '?')}",
    "",
    f"  Title:     {candidate.get('draft_title', '')}",
    f"  Statement: {candidate.get('draft_statement', '')}",
    f"  Tags:      {tags}",
    f"  Sources:   {n_sources} items",
  ]

  merge_with = candidate.get("merge_with")
  if merge_with:
    sim = candidate.get("dedup_similarity")
    sim_str = f" (sim {sim:.2f})" if sim is not None else ""
    lines.append(f"  Merge:     merge -> {merge_with}{sim_str}")

  lines.append("")
  return "\n".join(lines)
