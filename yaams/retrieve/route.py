"""Map a ParsedQuery onto a HybridQueryConfig.

Pure function. No DB, no LLM. Caller passes a baseline config (already
populated with explicit user flags) and gets back a copy with shape-driven
adjustments applied. Explicit user flags always win over parsed inference.
"""

from __future__ import annotations

from datetime import timedelta

from dataclasses import replace
from typing import Iterable

from yaams.retrieve.hybrid import HybridQueryConfig
from yaams.retrieve.parse import ParsedQuery

SYNTHESIS_TOP_K = 12
EVENT_TOP_K = 8
TIER2_BOOST_PREFERRED = 1.6
TIER2_BOOST_RAW = 1.0
EVENT_CONS_BOOST = 1.3
# Date-anchored queries ("4 april 2026", "27 april mandag UNE") want the atomic
# daily item, not the broad session consolidation that shares the date. The
# default consolidation boost out-promotes the day-item even when it tops both
# indices, so de-boost consolidations for temporal shapes.
TEMPORAL_CONS_BOOST = 0.85
TEMPORAL_NARROW = timedelta(days=3)
# Shapes that sort by timestamp rather than relevance. A soft entity *boost*
# is invisible to a timestamp sort, so these must keep the hard entity filter;
# intent terms narrow *within* the entity set (FTS scoring + relevance floor).
OCCURRENCE_SHAPES = frozenset({"first_occurrence", "last_occurrence"})
# first/last_occurrence sort by time, so a weak, tangential entity match would
# win on date alone. Keep only candidates within this fraction of the top
# relevance score before the timestamp sort. Conservative — cuts the clearly
# off-topic tail without dropping moderate genuine matches.
OCCURRENCE_RELEVANCE_FLOOR = 0.2


def route(
  parsed: ParsedQuery,
  base: HybridQueryConfig,
  *,
  explicit_since: bool = False,
  explicit_until: bool = False,
  explicit_sort: bool = False,
  self_identities: list[str] | None = None,
) -> HybridQueryConfig:
  cfg = replace(base)
  # Forward query shape so _hydrate_item can gate shape-specific credits.
  cfg.query_shape = parsed.shape or "factual"

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
    cfg.relevance_floor = OCCURRENCE_RELEVANCE_FLOOR
    if self_identities:
      cfg.participant_filter = list(self_identities)
  elif parsed.shape == "last_occurrence" and not explicit_sort:
    cfg.sort = "desc"
    cfg.relevance_floor = OCCURRENCE_RELEVANCE_FLOOR
    if self_identities:
      cfg.participant_filter = list(self_identities)
  elif parsed.shape == "event_anchored":
    cfg.top_k = min(cfg.top_k, EVENT_TOP_K) if cfg.top_k > EVENT_TOP_K else cfg.top_k
    cfg.consolidation_boost = max(cfg.consolidation_boost, EVENT_CONS_BOOST)
  elif parsed.shape == "temporal_range":
    # Only de-boost for a *narrow* window: "4 april 2026" wants that day's item,
    # but "aktivitet mai 2026" (a month) is a summary question whose right answer
    # is the session consolidation — leave that one boosted.
    if start is not None and end is not None and (end - start) <= TEMPORAL_NARROW:
      cfg.consolidation_boost = TEMPORAL_CONS_BOOST

  if parsed.high_quality:
    cfg.high_quality = True

  if parsed.sort != "relevance" and cfg.sort == "relevance" and not explicit_sort:
    cfg.sort = parsed.sort

  if parsed.entities:
    keep_hard_filter = (
      parsed.shape == "synthesis"
      or parsed.shape in OCCURRENCE_SHAPES
      or not parsed.topic_terms
    )
    if keep_hard_filter:
      # Keep the hard candidate allowlist when: synthesis (needs clean scoping
      # to avoid mixing unrelated sources); occurrence (timestamp sort ignores a
      # soft boost, so the entity must constrain the set); or no intent terms
      # (the entity is the only signal).
      cfg.entity_filter = list(parsed.entities)
    else:
      # A relevance-ranked query carrying intent terms (e.g. "incidents"): let
      # the terms drive the candidate set and merely *lift* entity-linked docs
      # via a soft boost, so a strong topic term isn't drowned by entity-matched
      # noise (generic M365 chatter).
      cfg.boost_entities = list(parsed.entities)
  else:
    cfg.entity_filter = None

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
