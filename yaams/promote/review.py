from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def format_note(candidate: dict[str, Any]) -> str:
  now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
  title = candidate.get("draft_title") or "Untitled"
  statement = candidate.get("draft_statement") or ""
  body = candidate.get("draft_body") or f"## Statement\n{statement}"
  tags_raw = candidate.get("draft_tags")
  if isinstance(tags_raw, str):
    import json as _json
    try:
      tags_list = _json.loads(tags_raw)
    except Exception:
      tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
  else:
    tags_list = list(tags_raw or [])

  tags_yaml = "\n".join(f"  - {t}" for t in tags_list) if tags_list else "  - general"

  return (
    f"---\n"
    f"created: {now}\n"
    f"updated: {now}\n"
    f"tags:\n{tags_yaml}\n"
    f"confidence: 0.7\n"
    f"source: yaams\n"
    f"scope: personal\n"
    f"lang: en\n"
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
  tags_raw = candidate.get("draft_tags")
  if isinstance(tags_raw, str):
    import json as _json
    try:
      tags = ", ".join(_json.loads(tags_raw))
    except Exception:
      tags = tags_raw
  else:
    tags = ", ".join(list(tags_raw or []))

  import json as _j
  try:
    ids = _j.loads(candidate.get("source_item_ids") or "[]")
    n_sources = len(ids)
  except Exception:
    n_sources = "?"

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
