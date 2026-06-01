"""Map a ParsedQuery onto a HybridQueryConfig.

Pure function. No DB, no LLM. Caller passes a baseline config (already
populated with explicit user flags) and gets back a copy with shape-driven
adjustments applied. Explicit user flags always win over parsed inference.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from yaams.retrieve.hybrid import HybridQueryConfig
from yaams.retrieve.parse import ParsedQuery

SYNTHESIS_TOP_K = 12
EVENT_TOP_K = 8
TIER2_BOOST_PREFERRED = 1.6
TIER2_BOOST_RAW = 1.0
EVENT_CONS_BOOST = 1.3


def route(
  parsed: ParsedQuery,
  base: HybridQueryConfig,
  *,
  explicit_since: bool = False,
  explicit_until: bool = False,
  explicit_sort: bool = False,
) -> HybridQueryConfig:
  cfg = replace(base)

  start, end = parsed.date_range
  if start is not None and not explicit_since and cfg.since is None:
    cfg.since = start
  if end is not None and not explicit_until and cfg.until is None:
    cfg.until = end

  if parsed.prefer_tier == "tier2_ledger":
    cfg.tier2_boost = max(cfg.tier2_boost, TIER2_BOOST_PREFERRED)
  elif parsed.prefer_tier == "raw":
    cfg.tier2_boost = TIER2_BOOST_RAW

  if parsed.shape == "synthesis":
    cfg.prefer_consolidations = True
    cfg.top_k = max(cfg.top_k, SYNTHESIS_TOP_K)
    cfg.high_quality = True
  elif parsed.shape == "first_occurrence" and not explicit_sort:
    cfg.sort = "asc"
  elif parsed.shape == "last_occurrence" and not explicit_sort:
    cfg.sort = "desc"
  elif parsed.shape == "event_anchored":
    cfg.top_k = min(cfg.top_k, EVENT_TOP_K) if cfg.top_k > EVENT_TOP_K else cfg.top_k
    cfg.consolidation_boost = max(cfg.consolidation_boost, EVENT_CONS_BOOST)

  if parsed.high_quality:
    cfg.high_quality = True

  if parsed.sort != "relevance" and cfg.sort == "relevance" and not explicit_sort:
    cfg.sort = parsed.sort

  cfg.entity_filter = list(parsed.entities) if parsed.entities else None

  return cfg


def filter_results_by_entities(
  results: Iterable,
  conn,
  entities: list[str] | None,
) -> list:
  """Drop hydrated results that share zero canonical entities with the
  parsed entity list. Items match via item_entities; consolidations
  match when any of their raw_item_ids share an entity link.

  Acts as a safety net behind the pre-retrieval entity filter in
  hybrid.query() - both must agree to avoid mixing matched and unrelated
  sources into synthesis.
  """
  results_list = list(results)
  if not entities:
    return results_list
  canonical_lookup = {e.lower() for e in entities}

  rows = conn.execute(
    """
    SELECT id FROM entities
    WHERE lower(canonical_name) IN (%s)
    """ % ",".join("?" * len(canonical_lookup)),
    tuple(canonical_lookup),
  ).fetchall()
  entity_ids = [row[0] if not hasattr(row, "keys") else row["id"] for row in rows]
  if not entity_ids:
    return []

  item_ids = [r.id for r in results_list if r.kind == "item"]
  cons_ids = [r.id for r in results_list if r.kind == "consolidation"]

  matched_items: set = set()
  if item_ids:
    placeholders_items = ",".join("?" * len(item_ids))
    placeholders_ents = ",".join("?" * len(entity_ids))
    matched = conn.execute(
      f"""
      SELECT DISTINCT item_id FROM item_entities
      WHERE item_id IN ({placeholders_items})
        AND entity_id IN ({placeholders_ents})
      """,
      (*item_ids, *entity_ids),
    ).fetchall()
    matched_items = {
      row[0] if not hasattr(row, "keys") else row["item_id"] for row in matched
    }

  matched_cons: set = set()
  if cons_ids:
    placeholders_cons = ",".join("?" * len(cons_ids))
    placeholders_ents = ",".join("?" * len(entity_ids))
    matched = conn.execute(
      f"""
      SELECT DISTINCT c.id
      FROM consolidations c, json_each(c.raw_item_ids) j
      JOIN item_entities ie ON ie.item_id = j.value
      WHERE c.id IN ({placeholders_cons})
        AND ie.entity_id IN ({placeholders_ents})
      """,
      (*cons_ids, *entity_ids),
    ).fetchall()
    matched_cons = {
      row[0] if not hasattr(row, "keys") else row["id"] for row in matched
    }

  return [
    r for r in results_list
    if (r.kind == "item" and r.id in matched_items)
    or (r.kind == "consolidation" and r.id in matched_cons)
  ]
