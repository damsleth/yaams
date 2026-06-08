from __future__ import annotations

import json as _json
import re
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
  inbox_path.mkdir(parents=True, exist_ok=True)
  filename = note_filename(candidate)
  dest = inbox_path / filename

  # avoid clobbering an existing file
  if dest.exists():
    ts = datetime.now(UTC).strftime("%H%M%S")
    dest = inbox_path / filename.replace(".md", f"_{ts}.md")

  dest.write_text(content or format_note(candidate), encoding="utf-8")
  return dest


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
    "",
  ]
  return "\n".join(lines)
