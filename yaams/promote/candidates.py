from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from yaams.synthesize.llm import LLMAdapter

GENERATE_PROMPT = """\
You are drafting an atomic note for a personal knowledge base.

Given the sources below about "{entity}", write a single atomic note.
Respond ONLY with valid YAML in this exact format (no markdown fences):

type: fact
title: Short descriptive title (5-8 words)
statement: One clear sentence - the core claim, preference, or concept.
tags:
  - tag1
  - tag2
body: |
  ## Statement
  Repeat the statement here.

  ## Detail
  - Key detail or date
  - Another supporting fact
  - Context or implication

Valid types: fact, preference, concept, goal.
Only include what is directly supported by the sources. Be specific and concise.
Write title, statement and body in the dominant language of the sources
(Norwegian or English). Keep the YAML keys and type values in English.

SOURCES:
{sources}
"""


@dataclass
class PromotionCandidate:
  id: str
  entity: str
  draft_type: str
  draft_title: str
  draft_statement: str
  draft_body: str
  draft_tags: list[str]
  source_item_ids: list[str]
  created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
  backend: str = ""
  model: str | None = None


@dataclass
class RejectedIndex:
  """Suppression signal sourced from cogled's `rejected_candidates.jsonl`.

  Supports the contract v1 match precedence (candidate id, then source-item
  overlap, then entity+title fallback for pre-v1 rejections). Empty when the
  log is missing/unreadable — degrade open, never raise."""

  candidate_ids: set[str] = field(default_factory=set)
  item_ids: set[str] = field(default_factory=set)
  # (lowercased entity, lowercased title) pairs from pre-v1 rejections that
  # lacked a candidate id and source item ids.
  entity_titles: set[tuple[str, str]] = field(default_factory=set)

  def __bool__(self) -> bool:
    return bool(self.candidate_ids or self.item_ids or self.entity_titles)


@dataclass
class PromoteConfig:
  window_days: int = 90
  window_days_by_type: dict[str, int] = field(default_factory=lambda: {"person": 365})
  min_cluster_items: int = 3
  cluster_fetch_k: int = 10
  note_index_path: Path | None = None
  rejected_log_path: Path | None = None


def generate_candidates(
  conn: sqlite3.Connection,
  adapter: LLMAdapter,
  config: PromoteConfig,
  entity_filter: str | None = None,
  on_progress: Callable[[str], None] | None = None,
) -> list[PromotionCandidate]:
  entities = _fetch_dict_entities(conn, config, entity_filter)
  existing_tier2 = _fetch_tier2_titles(conn)
  index_texts = _load_index_texts(config.note_index_path)
  rejected = _load_rejected(config.rejected_log_path)
  candidates: list[PromotionCandidate] = []
  total = len(entities)

  for i, (entity_name, entity_id, window_days) in enumerate(entities, 1):
    if on_progress:
      on_progress(f"[{i}/{total}] {entity_name} ...")
    if _is_covered(entity_name, existing_tier2, index_texts):
      if on_progress:
        on_progress("  skipped (already in Tier 2)")
      continue
    cluster = _fetch_cluster(conn, entity_id, config, window_days)
    if len(cluster) < config.min_cluster_items:
      if on_progress:
        on_progress(f"  skipped (cluster too small: {len(cluster)} items)")
      continue
    cluster_item_ids = [r["id"] for r in cluster]
    # Rejection precedence rules 1 & 2 are decidable pre-draft (no title yet),
    # so we short-circuit before the expensive LLM call.
    if rejected:
      cid = _candidate_id(entity_name, cluster_item_ids)
      if cid in rejected.candidate_ids:
        if on_progress:
          on_progress("  skipped (previously rejected)")
        continue
      if any(item_id in rejected.item_ids for item_id in cluster_item_ids):
        if on_progress:
          on_progress("  skipped (previously rejected)")
        continue
    candidate = _draft(adapter, entity_name, cluster)
    if candidate:
      # Rule 3: entity + title fallback, only for pre-v1 rejections that
      # lacked id/item-ids. Requires the drafted title, so it runs post-draft.
      if rejected.entity_titles and _rejected_by_entity_title(rejected, candidate):
        if on_progress:
          on_progress("  skipped (previously rejected)")
        continue
      candidates.append(candidate)
      if on_progress:
        on_progress(f"  drafted: {candidate.draft_title}")
    elif on_progress:
      on_progress("  LLM draft failed")

  return candidates


def store_candidates(
  conn: sqlite3.Connection,
  candidates: list[PromotionCandidate],
) -> int:
  stored = 0
  for c in candidates:
    try:
      conn.execute(
        """
        INSERT OR IGNORE INTO promotion_candidates
          (id, created_at, entity, draft_type, draft_title, draft_statement,
           draft_body, draft_tags, source_item_ids, status, backend, model)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
          c.id,
          c.created_at.isoformat(),
          c.entity,
          c.draft_type,
          c.draft_title,
          c.draft_statement,
          c.draft_body,
          json.dumps(c.draft_tags),
          json.dumps(c.source_item_ids),
          "pending",
          c.backend,
          c.model,
        ),
      )
      stored += 1
    except sqlite3.IntegrityError:
      pass
  conn.commit()
  return stored


def fetch_pending(
  conn: sqlite3.Connection,
  status: str = "pending",
) -> list[dict[str, Any]]:
  rows = conn.execute(
    """
    SELECT * FROM promotion_candidates
    WHERE (? = 'all' OR status = ?)
    ORDER BY created_at DESC
    """,
    (status, status),
  ).fetchall()
  return [dict(r) for r in rows]


def update_status(
  conn: sqlite3.Connection,
  candidate_id: str,
  status: str,
  promoted_path: str | None = None,
) -> None:
  now = datetime.now(UTC).isoformat()
  conn.execute(
    """
    UPDATE promotion_candidates
    SET status = ?, reviewed_at = ?, promoted_path = ?
    WHERE id = ?
    """,
    (status, now, promoted_path, candidate_id),
  )
  conn.commit()


def mark_items_promoted(
  conn: sqlite3.Connection,
  item_ids: list[str],
  promoted_path: str,
) -> None:
  for item_id in item_ids:
    conn.execute(
      "UPDATE items SET promoted_to = ? WHERE id = ?",
      (promoted_path, item_id),
    )
  conn.commit()


_ENTITY_QUERY = """
  SELECT e.canonical_name, e.id, count(*) AS cnt
  FROM item_entities ie
  JOIN entities e ON e.id = ie.entity_id
  JOIN items i ON i.id = ie.item_id
  WHERE ie.source = 'dictionary'
    AND i.source NOT IN ('tier2_ledger')
    AND i.timestamp >= ?
    AND {type_filter}
    AND (? IS NULL OR e.canonical_name = ?)
    AND lower(e.canonical_name) NOT IN (
      SELECT lower(subject) FROM items
      WHERE source = 'tier2_ledger' AND subject IS NOT NULL
    )
  GROUP BY e.id
  HAVING cnt >= ?
  ORDER BY cnt DESC
"""


def _fetch_dict_entities(
  conn: sqlite3.Connection,
  config: PromoteConfig,
  entity_filter: str | None,
) -> list[tuple[str, int, int]]:
  seen: dict[int, tuple[str, int, int]] = {}

  # Per-type windows (e.g. person -> 365 days)
  for etype, days in config.window_days_by_type.items():
    sql = _ENTITY_QUERY.format(type_filter="e.entity_type = ?")
    for r in conn.execute(sql, (
      _cutoff_iso(days), etype, entity_filter, entity_filter, config.min_cluster_items
    )).fetchall():
      seen.setdefault(r["id"], (r["canonical_name"], r["id"], days))

  # Default window for types not explicitly configured
  known = list(config.window_days_by_type.keys())
  if known:
    placeholders = ",".join("?" * len(known))
    sql = _ENTITY_QUERY.format(type_filter=f"e.entity_type NOT IN ({placeholders})")
    params = (_cutoff_iso(config.window_days), *known, entity_filter, entity_filter, config.min_cluster_items)
  else:
    sql = _ENTITY_QUERY.format(type_filter="1=1")
    params = (_cutoff_iso(config.window_days), entity_filter, entity_filter, config.min_cluster_items)
  for r in conn.execute(sql, params).fetchall():
    seen.setdefault(r["id"], (r["canonical_name"], r["id"], config.window_days))

  return list(seen.values())


def _fetch_tier2_titles(conn: sqlite3.Connection) -> list[str]:
  rows = conn.execute(
    "SELECT subject FROM items WHERE source = 'tier2_ledger' AND subject IS NOT NULL"
  ).fetchall()
  return [r["subject"].lower() for r in rows]


def _load_index_texts(index_path: Path | None) -> list[str]:
  if not index_path:
    return []
  try:
    index = json.loads(Path(index_path).expanduser().read_text(encoding="utf-8"))
    texts: list[str] = []
    for entry in (index.get("entries") or {}).values():
      c = entry.get("candidate") or {}
      for field in ("title", "statement"):
        v = (c.get(field) or "").lower()
        if v:
          texts.append(v)
    return texts
  except Exception:
    return []


def _candidate_id(entity_name: str, item_ids: list[str]) -> str:
  """Stable 16-hex id for a candidate, keyed on entity + its source item ids.

  This is the rejection-log match key (contract v1); cogled records it as
  `yaams_candidate_id`."""
  return sha256(f"{entity_name}:{','.join(item_ids)}".encode()).hexdigest()[:16]


def _load_rejected(path: Path | None) -> RejectedIndex:
  """Load cogled's `rejected_candidates.jsonl` into a suppression index.

  Missing/unreadable/None file → empty index (suppresses nothing). Malformed
  lines are skipped, never fatal — degrade open."""
  rejected = RejectedIndex()
  if not path:
    return rejected
  try:
    raw = Path(path).expanduser().read_text(encoding="utf-8")
  except Exception:
    return rejected
  for line in raw.splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      rec = json.loads(line)
    except Exception:
      continue
    if not isinstance(rec, dict):
      continue
    cid = rec.get("yaams_candidate_id") or ""
    if cid:
      rejected.candidate_ids.add(cid)
    item_ids = rec.get("yaams_source_item_ids") or []
    if isinstance(item_ids, list):
      for item_id in item_ids:
        if item_id:
          rejected.item_ids.add(str(item_id))
    # Rule-3 fallback only applies to pre-v1 rejections that carried neither a
    # candidate id nor source item ids.
    if not cid and not item_ids:
      entity = (rec.get("yaams_entity") or "").strip().lower()
      title = (rec.get("title") or "").strip().lower()
      if entity and title:
        rejected.entity_titles.add((entity, title))
  return rejected


def _rejected_by_entity_title(rejected: RejectedIndex, candidate: PromotionCandidate) -> bool:
  """Rule-3 fallback: same entity AND one title is a substring of the other
  (case-insensitive). Matches the documented pre-v1 degradation path."""
  entity = candidate.entity.strip().lower()
  draft_title = candidate.draft_title.strip().lower()
  if not entity or not draft_title:
    return False
  for rej_entity, rej_title in rejected.entity_titles:
    if rej_entity != entity:
      continue
    if rej_title and (rej_title in draft_title or draft_title in rej_title):
      return True
  return False


def _is_covered(
  entity_name: str,
  existing_titles: list[str],
  index_texts: list[str] | None = None,
) -> bool:
  needle = entity_name.lower()
  if any(needle in t for t in existing_titles):
    return True
  if index_texts and any(needle in t for t in index_texts):
    return True
  return False


def _fetch_cluster(
  conn: sqlite3.Connection,
  entity_id: int,
  config: PromoteConfig,
  window_days: int | None = None,
) -> list[dict[str, Any]]:
  effective_window = window_days if window_days is not None else config.window_days
  cutoff = _cutoff_iso(effective_window)
  rows = conn.execute(
    """
    SELECT i.id, i.source, i.timestamp, i.sender, i.content, i.subject
    FROM item_entities ie
    JOIN items i ON i.id = ie.item_id
    WHERE ie.entity_id = ?
      AND i.source NOT IN ('tier2_ledger')
      AND i.timestamp >= ?
    ORDER BY i.timestamp DESC
    LIMIT ?
    """,
    (entity_id, cutoff, config.cluster_fetch_k),
  ).fetchall()
  return [dict(r) for r in rows]


def _draft(
  adapter: LLMAdapter,
  entity_name: str,
  cluster: list[dict[str, Any]],
) -> PromotionCandidate | None:
  sources_text = "\n\n".join(
    f"[{i+1}] {r['source']} {r['timestamp'][:10]}\n{(r['content'] or '')[:400]}"
    for i, r in enumerate(cluster)
  )
  prompt = GENERATE_PROMPT.format(entity=entity_name, sources=sources_text)
  try:
    response = adapter.complete(prompt, max_tokens=600, temperature=0.2)
  except Exception:
    return None

  parsed = _parse_yaml_response(response.text)
  if not parsed:
    return None

  item_ids = [r["id"] for r in cluster]
  cid = _candidate_id(entity_name, item_ids)

  return PromotionCandidate(
    id=cid,
    entity=entity_name,
    draft_type=parsed.get("type", "fact"),
    draft_title=str(parsed.get("title", entity_name)),
    draft_statement=str(parsed.get("statement", "")),
    draft_body=str(parsed.get("body", "")),
    draft_tags=_extract_tags(parsed.get("tags", [])),
    source_item_ids=item_ids,
    backend=response.backend,
    model=response.model,
  )


def _parse_yaml_response(text: str) -> dict[str, Any] | None:
  try:
    import yaml
    cleaned = re.sub(r"^```ya?ml\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*$", "", cleaned)
    data = yaml.safe_load(cleaned)
    if isinstance(data, dict) and "statement" in data:
      return data
  except Exception:
    pass
  return None


def _extract_tags(raw: Any) -> list[str]:
  if isinstance(raw, list):
    return [str(t) for t in raw]
  if isinstance(raw, str):
    return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
  return []


def _cutoff_iso(window_days: int) -> str:
  from datetime import timedelta
  return (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
