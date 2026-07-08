"""Hybrid retrieval over YAAMS items and consolidations.

Vector search via sqlite-vec + sparse search via FTS5, fused with
reciprocal rank fusion. No LLM, no query parsing - phase B v0.

Result schema deliberately echoes the cognitive-ledger's ScoredResult
fields where they overlap (id, source, score, components) so a future
Phase F fusion layer can merge results across the two tiers cheaply.
"""

from __future__ import annotations

import json
import sqlite3
from array import array
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable, Sequence, cast

from yaams.retrieve.synonyms import expand_fts_tokens, load_synonym_groups
from yaams.time import ensure_utc

DEFAULT_TOP_K = 20
DEFAULT_PER_INDEX_K = 50
RRF_K = 30
HIGH_QUALITY_FETCH_MULTIPLIER = 2
TIMESTAMP_SORT_FETCH_MULTIPLIER = 4
ENTITY_FILTER_FETCH_MULTIPLIER = 4
# Per-field bm25 weights (FTS5 bm25() takes one weight per column, in
# table-declaration order; UNINDEXED columns get 0).
FTS_ITEM_WEIGHTS = (0.0, 1.0, 2.0, 1.0)  # item_id, content, subject, sender
FTS_CONS_WEIGHTS = (0.0, 1.0, 1.0)  # consolidation_id, summary, participants

# Precision-with-use feedback boost (cfg.feedback_boost). Per-signal weight and
# cap mirror the display-side validation boost in retrieve.trust so the two
# stay consistent. The cap is the guardrail against citation self-reinforcement
# (a result cited because it ranked #1 can lift its own rank by at most CAP).
FEEDBACK_BOOST_PER = 0.03
FEEDBACK_BOOST_CAP = 0.15


@dataclass
class HybridQueryConfig:
  top_k: int = DEFAULT_TOP_K
  per_index_k: int = DEFAULT_PER_INDEX_K
  include_items: bool = True
  include_consolidations: bool = True
  prefer_consolidations: bool = True
  source_filter: list[str] | None = None
  since: datetime | None = None
  until: datetime | None = None
  sender_filter: list[str] | None = None
  rrf_k: int = RRF_K
  consolidation_boost: float = 1.05
  tier2_source: str = "tier2_ledger"
  tier2_boost: float = 1.2
  sort: str = "relevance"
  high_quality: bool = False
  entity_filter: list[str] | None = None
  expand_synonyms: bool = True
  synonyms: dict[str, list[str]] | None = None
  synonym_groups: list[list[str]] | None = None
  # Map of lowercased canonical entity name -> association weight in (0, 1].
  # Query entities sit at 1.0; associated entities carry their merged weight.
  # When set, hydrated scores are multiplied by the result's best weight so
  # associated-only documents are surfaced but never outrank exact matches.
  assoc_weights: dict[str, float] | None = None
  # Soft metadata boost: documents linked to any of these entities have their
  # score multiplied by boost_factor (does not filter the candidate set).
  boost_entities: list[str] | None = None
  boost_factor: float = 3.0
  lang_filter: str | None = None
  # Restrict candidates to items the user took part in — sender or a recipient
  # matches one of these identities (casefolded), consolidations match via
  # participants. Set for first/last_occurrence so "when did I first/last …"
  # anchors on the user's own activity, not any corpus mention of the entity.
  participant_filter: list[str] | None = None
  # For timestamp-sorted occurrence queries, drop candidates scoring below this
  # fraction of the top relevance score *before* sorting by time, so a weak,
  # tangential match can't win first/last just by being the oldest/newest.
  # 0 disables. Set by route() for first/last_occurrence, not by explicit sort.
  relevance_floor: float = 0.0
  # Query shape forwarded from ParsedQuery so _hydrate_item can gate
  # shape-specific credits (e.g. tier2_factual_coverage_recovery).
  query_shape: str = "factual"
  # Additive RRF credit for FTS-present/vector-absent tier2 factual items.
  # Sized as GAMMA/(rrf_k + fts_rank + 1); sweep [0.5, 0.8, 1.0].
  tier2_factual_coverage_gamma: float = 0.5
  # Opt-in cross-encoder rerank over the hydrated pool. Off by default so the
  # fast path (and --no-vector) is byte-for-byte unchanged. When enabled, the
  # top `rerank_k` hydrated candidates are re-scored by the cross-encoder and
  # that score replaces the RRF score before the final sort.
  rerank_enabled: bool = False
  reranker_model: str = "BAAI/bge-reranker-v2-m3"
  rerank_k: int = 50
  # Reranker device. cpu by default: these cross-encoders crash mid-predict on
  # Apple mps and a small pool is fast on CPU. Set cuda on a GPU box.
  reranker_device: str | None = "cpu"
  # Precision-with-use: multiply a result's score by a capped factor derived
  # from accumulated real usage — +PER·citations (automatic positive from
  # logged answers) and −PER·corrections (human negative), each capped at CAP.
  # Off by default; enable via `retrieve.feedback_boost: true` only after real
  # query logs accumulate and the frozen-fixture eval gate passes. Naturally a
  # no-op until signals exist (factor = 1.0). See .plans/retrieval-flywheel.md.
  feedback_boost: bool = False
  # Leave-one-out: drop this query's own citations/corrections from the boost.
  # Set by the eval harness so a replayed gold query can't boost itself (a live
  # query has no self-feedback yet either). None in normal use.
  feedback_boost_exclude_query_id: str | None = None


@dataclass
class ScoreComponents:
  vector_rank: int | None = None
  vector_distance: float | None = None
  fts_rank: int | None = None
  fts_score: float | None = None
  rrf_score: float = 0.0


@dataclass
class HybridResult:
  id: str
  kind: str
  source: str
  timestamp: datetime
  sender: str
  subject: str
  content: str
  thread_id: str | None
  score: float
  components: ScoreComponents = field(default_factory=ScoreComponents)
  participants: list[str] = field(default_factory=list)
  item_count: int = 1
  # Association weight in (0, 1]: 1.0 for an exact entity match, < 1.0 for a
  # result reached only through an associated entity. Drives the exact-before-
  # associated partition so associated docs never outrank exact ones.
  assoc_weight: float = 1.0
  # Display-only trust verdict (yaams.trust.TrustVerdict | None); never feeds
  # ranking. None until populated post-scoring by attach_trust_verdicts. Typed
  # loosely to avoid importing yaams.trust into the hot retrieval path.
  trust: object | None = None


def query(
  conn: sqlite3.Connection,
  text: str,
  embedding: object | None = None,
  config: HybridQueryConfig | None = None,
) -> list[HybridResult]:
  cfg = config or HybridQueryConfig()
  if not text or not text.strip():
    return []

  if cfg.expand_synonyms and cfg.synonyms is None:
    cfg.synonyms = load_synonym_groups(conn, cfg.synonym_groups)

  fetch_k = cfg.per_index_k
  if cfg.high_quality:
    fetch_k = max(fetch_k, cfg.per_index_k * HIGH_QUALITY_FETCH_MULTIPLIER)
  if cfg.sort != "relevance":
    fetch_k = max(fetch_k, cfg.per_index_k * TIMESTAMP_SORT_FETCH_MULTIPLIER)
  if cfg.entity_filter:
    fetch_k = max(fetch_k, cfg.per_index_k * ENTITY_FILTER_FETCH_MULTIPLIER)
  if cfg.participant_filter:
    fetch_k = max(fetch_k, cfg.per_index_k * ENTITY_FILTER_FETCH_MULTIPLIER)
  fetch_cfg = replace(cfg, per_index_k=fetch_k) if fetch_k != cfg.per_index_k else cfg

  item_allow: set[str] | None = None
  cons_allow: set[str] | None = None
  if cfg.entity_filter:
    item_allow, cons_allow = _resolve_entity_allowlist(conn, cfg.entity_filter)

  part_item_allow: set[str] | None = None
  part_cons_allow: set[str] | None = None
  if cfg.participant_filter:
    part_item_allow, part_cons_allow = _resolve_participant_allowlist(
      conn, cfg.participant_filter
    )

  fts_items: list[tuple[str, str, int, float]] = []
  fts_cons: list[tuple[str, str, int, float]] = []
  vec_items: list[tuple[str, str, int, float]] = []
  vec_cons: list[tuple[str, str, int, float]] = []

  if cfg.include_items:
    fts_items = _fts_search_items(conn, text, fetch_cfg)
    if embedding is not None:
      vec_items = _vec_search_items(conn, embedding, fetch_cfg)
  if cfg.include_consolidations:
    fts_cons = _fts_search_consolidations(conn, text, fetch_cfg)
    if embedding is not None:
      vec_cons = _vec_search_consolidations(conn, embedding, fetch_cfg)

  if item_allow is not None:
    fts_items = [t for t in fts_items if t[1] in item_allow]
    vec_items = [t for t in vec_items if t[1] in item_allow]
  if cons_allow is not None:
    fts_cons = [t for t in fts_cons if t[1] in cons_allow]
    vec_cons = [t for t in vec_cons if t[1] in cons_allow]
  if part_item_allow is not None:
    fts_items = [t for t in fts_items if t[1] in part_item_allow]
    vec_items = [t for t in vec_items if t[1] in part_item_allow]
  if part_cons_allow is not None:
    fts_cons = [t for t in fts_cons if t[1] in part_cons_allow]
    vec_cons = [t for t in vec_cons if t[1] in part_cons_allow]

  fused = _fuse(
    [fts_items, fts_cons, vec_items, vec_cons],
    cfg=cfg,
  )
  hydrate_cap = max(cfg.top_k * 2, fetch_k)
  hydrated = _hydrate(conn, fused, cfg, hydrate_cap=hydrate_cap)
  if (
    not hydrated
    and (cfg.since is not None or cfg.until is not None)
    and not cfg.entity_filter
    and not cfg.participant_filter
  ):
    # Browse fallback: a time-windowed query whose text matched nothing in
    # either index ("list all new items last 24 hrs", "torsdag 14 mai") still
    # wants the items *in that window*, sorted by time — not zero results.
    # Fires only when we'd otherwise return nothing, so it can never displace
    # a real match. Skipped when an entity/participant filter is set: there the
    # user asked for a specific thing, and a whole-window dump would be noise.
    hydrated = _browse_window(conn, cfg, cap=hydrate_cap)
  if cfg.rerank_enabled and hydrated:
    # Opt-in cross-encoder rerank: re-score the top `rerank_k` candidates and
    # let the cross-encoder score replace the RRF score. The pool becomes the
    # result set (the tail beyond rerank_k can't outrank a reranked candidate),
    # then the existing boost/assoc/sort below apply on top. Imported lazily so
    # the default path never loads the cross-encoder.
    from yaams.retrieve.rerank import candidate_text, rerank_pairs
    pool = hydrated[: cfg.rerank_k]
    pairs = [(text, candidate_text(r.subject, r.content)) for r in pool]
    scores = rerank_pairs(text, pairs, cfg.reranker_model, device=cfg.reranker_device)
    for r, s in zip(pool, scores):
      r.score = s
    hydrated = pool
  if cfg.boost_entities:
    # Soft metadata boost: lift documents tagged with a matching entity
    # without removing anything else from the result set.
    item_b, cons_b = _resolve_entity_allowlist(conn, cfg.boost_entities)
    for r in hydrated:
      if r.id in (item_b if r.kind == "item" else cons_b):
        r.score *= cfg.boost_factor
  if cfg.feedback_boost and hydrated:
    # Precision-with-use: lift results that real usage proved good — cited by an
    # answer, or named by a human correction as the right (mis-ranked) doc. Both
    # are positive. Capped so citation feedback can't runaway (no per-doc
    # negative yet — that's a P3 item).
    from yaams.signals import result_boost_counts
    counts = result_boost_counts(
      conn, [r.id for r in hydrated],
      exclude_query_id=cfg.feedback_boost_exclude_query_id,
    )
    for r in hydrated:
      pos = counts.get(r.id, 0)
      if pos:
        r.score *= 1.0 + min(FEEDBACK_BOOST_PER * pos, FEEDBACK_BOOST_CAP)
  if cfg.assoc_weights:
    item_w, cons_w = _assoc_weight_maps(conn, cfg.assoc_weights)
    for r in hydrated:
      weight = (item_w if r.kind == "item" else cons_w).get(r.id, 1.0)
      r.assoc_weight = weight
      if weight != 1.0:
        r.score *= weight
  if cfg.sort in ("asc", "desc"):
    hydrated = _apply_relevance_floor(hydrated, cfg.relevance_floor)
    hydrated.sort(key=lambda r: (r.timestamp, -r.score), reverse=cfg.sort == "desc")
  else:
    hydrated.sort(key=lambda r: r.score, reverse=True)
  if cfg.assoc_weights:
    # Stable partition AFTER the chosen ordering: exact entity matches
    # (weight 1.0) always sit above associated-only results, whatever their
    # score or recency. This enforces the "never outrank exact matches"
    # contract that a plain score multiply + timestamp sort cannot.
    hydrated.sort(key=lambda r: r.assoc_weight < 1.0)
  return hydrated[: cfg.top_k]


def _resolve_entity_allowlist(
  conn: sqlite3.Connection,
  entity_names: list[str],
) -> tuple[set[str], set[str]]:
  """Return (item_ids, consolidation_ids) that share at least one of the
  named canonical entities. Consolidations match via raw_item_ids."""
  if not entity_names:
    return set(), set()
  canonical_lower = [e.lower() for e in entity_names if e]
  if not canonical_lower:
    return set(), set()
  ent_ph = ",".join("?" * len(canonical_lower))
  ent_rows = conn.execute(
    f"SELECT id FROM entities WHERE lower(canonical_name) IN ({ent_ph})",
    tuple(canonical_lower),
  ).fetchall()
  entity_ids = [
    r[0] if not hasattr(r, "keys") else r["id"] for r in ent_rows
  ]
  if not entity_ids:
    return set(), set()
  ent_id_ph = ",".join("?" * len(entity_ids))
  item_rows = conn.execute(
    f"SELECT DISTINCT item_id FROM item_entities WHERE entity_id IN ({ent_id_ph})",
    tuple(entity_ids),
  ).fetchall()
  item_ids: set[str] = {
    r[0] if not hasattr(r, "keys") else r["item_id"] for r in item_rows
  }
  cons_ids: set[str] = set()
  if item_ids:
    item_ph = ",".join("?" * len(item_ids))
    cons_rows = conn.execute(
      f"""
      SELECT DISTINCT c.id
      FROM consolidations c, json_each(c.raw_item_ids) j
      WHERE j.value IN ({item_ph})
      """,
      tuple(item_ids),
    ).fetchall()
    cons_ids = {
      r[0] if not hasattr(r, "keys") else r["id"] for r in cons_rows
    }
  return item_ids, cons_ids


def _resolve_participant_allowlist(
  conn: sqlite3.Connection,
  identities: list[str],
) -> tuple[set[str], set[str]]:
  """Return (item_ids, consolidation_ids) the user took part in.

  An item matches when its ``sender`` is one of ``identities`` or one of its
  ``recipients`` is; a consolidation matches when any of its ``participants``
  is. All comparisons are casefolded. Returns empty sets when no identity is
  given, which the caller treats as "match nothing"."""
  ids = [i.strip().lower() for i in identities if i and i.strip()]
  if not ids:
    return set(), set()
  ph = ",".join("?" * len(ids))
  item_rows = conn.execute(
    f"""
    SELECT id FROM items
    WHERE lower(sender) IN ({ph})
       OR EXISTS (
         SELECT 1 FROM json_each(items.recipients) j
         WHERE lower(j.value) IN ({ph})
       )
    """,
    (*ids, *ids),
  ).fetchall()
  item_ids: set[str] = {
    r[0] if not hasattr(r, "keys") else r["id"] for r in item_rows
  }
  cons_rows = conn.execute(
    f"""
    SELECT id FROM consolidations
    WHERE EXISTS (
      SELECT 1 FROM json_each(consolidations.participants) j
      WHERE lower(j.value) IN ({ph})
    )
    """,
    tuple(ids),
  ).fetchall()
  cons_ids: set[str] = {
    r[0] if not hasattr(r, "keys") else r["id"] for r in cons_rows
  }
  return item_ids, cons_ids


def _assoc_weight_maps(
  conn: sqlite3.Connection,
  assoc_weights: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
  """Map item ids and consolidation ids to the best association weight of any
  entity they carry. Used to multiply hydrated scores: a document tagged with
  a query entity keeps weight 1.0, one tagged only with an associated entity
  is scaled down by that association's weight."""
  names_lower = list(assoc_weights.keys())
  if not names_lower:
    return {}, {}
  ph = ",".join("?" * len(names_lower))
  id_weight: dict[int, float] = {}
  for row in conn.execute(
    f"SELECT id, lower(canonical_name) AS nm FROM entities "
    f"WHERE lower(canonical_name) IN ({ph})",
    tuple(names_lower),
  ):
    eid = row[0] if not hasattr(row, "keys") else row["id"]
    nm = row[1] if not hasattr(row, "keys") else row["nm"]
    id_weight[eid] = assoc_weights[nm]
  if not id_weight:
    return {}, {}

  ent_ph = ",".join("?" * len(id_weight))
  item_w: dict[str, float] = {}
  for row in conn.execute(
    f"SELECT item_id, entity_id FROM item_entities WHERE entity_id IN ({ent_ph})",
    tuple(id_weight.keys()),
  ):
    iid = row[0] if not hasattr(row, "keys") else row["item_id"]
    eid = row[1] if not hasattr(row, "keys") else row["entity_id"]
    weight = id_weight[eid]
    if weight > item_w.get(iid, 0.0):
      item_w[iid] = weight

  cons_w: dict[str, float] = {}
  if item_w:
    item_ph = ",".join("?" * len(item_w))
    for row in conn.execute(
      f"""
      SELECT c.id AS cid, j.value AS iid
      FROM consolidations c, json_each(c.raw_item_ids) j
      WHERE j.value IN ({item_ph})
      """,
      tuple(item_w.keys()),
    ):
      cid = row[0] if not hasattr(row, "keys") else row["cid"]
      iid = row[1] if not hasattr(row, "keys") else row["iid"]
      weight = item_w.get(iid, 0.0)
      if weight > cons_w.get(cid, 0.0):
        cons_w[cid] = weight
  return item_w, cons_w


def _fts_search_items(
  conn: sqlite3.Connection,
  text: str,
  cfg: HybridQueryConfig,
) -> list[tuple[str, str, int, float]]:
  match = _fts_query(text, cfg.synonyms)
  if not match:
    return []
  item_w = ", ".join(str(w) for w in FTS_ITEM_WEIGHTS)
  rows = conn.execute(
    f"""
    SELECT items_fts.item_id AS id, bm25(items_fts, {item_w}) AS score
    FROM items_fts
    JOIN items ON items.id = items_fts.item_id
    WHERE items_fts MATCH ?
      AND (? = '' OR items.source IN (SELECT value FROM json_each(?)))
      AND (? IS NULL OR items.timestamp >= ?)
      AND (? IS NULL OR items.timestamp <= ?)
      AND (? IS NULL OR items.lang = ?)
      AND (? = 0 OR items.timestamp_inferred = 0)
      AND items.consolidated_into IS NULL
    ORDER BY score
    LIMIT ?
    """,
    _filter_params(match, cfg)
    + (cfg.lang_filter, cfg.lang_filter, _exclude_inferred(cfg), cfg.per_index_k),
  ).fetchall()
  return [
    ("item", row["id"], rank, float(row["score"]))
    for rank, row in enumerate(rows)
  ]


def _fts_search_consolidations(
  conn: sqlite3.Connection,
  text: str,
  cfg: HybridQueryConfig,
) -> list[tuple[str, str, int, float]]:
  match = _fts_query(text, cfg.synonyms)
  if not match:
    return []
  rows = conn.execute(
    """
    SELECT consolidations_fts.consolidation_id AS id, bm25(consolidations_fts) AS score
    FROM consolidations_fts
    JOIN consolidations ON consolidations.id = consolidations_fts.consolidation_id
    WHERE consolidations_fts MATCH ?
      AND (? = '' OR consolidations.source IN (SELECT value FROM json_each(?)))
      AND (? IS NULL OR consolidations.start_timestamp >= ?)
      AND (? IS NULL OR consolidations.end_timestamp <= ?)
      AND (? IS NULL OR EXISTS (
            SELECT 1 FROM items i, json_each(consolidations.raw_item_ids) j
            WHERE i.id = j.value AND i.lang = ?))
    ORDER BY score
    LIMIT ?
    """,
    _filter_params(match, cfg) + (cfg.lang_filter, cfg.lang_filter, cfg.per_index_k),
  ).fetchall()
  return [
    ("consolidation", row["id"], rank, float(row["score"]))
    for rank, row in enumerate(rows)
  ]


def _vec_search_items(
  conn: sqlite3.Connection,
  embedding: object,
  cfg: HybridQueryConfig,
) -> list[tuple[str, str, int, float]]:
  blob = _embedding_to_blob(embedding)
  rows = conn.execute(
    """
    SELECT items_vec.item_id AS id, distance
    FROM items_vec
    JOIN items ON items.id = items_vec.item_id
    WHERE items_vec.embedding MATCH ?
      AND k = ?
      AND (? = '' OR items.source IN (SELECT value FROM json_each(?)))
      AND (? IS NULL OR items.timestamp >= ?)
      AND (? IS NULL OR items.timestamp <= ?)
      AND (? IS NULL OR items.lang = ?)
      AND (? = 0 OR items.timestamp_inferred = 0)
      AND items.consolidated_into IS NULL
    ORDER BY distance
    """,
    (blob, cfg.per_index_k)
    + _vec_filter_params(cfg)
    + (cfg.lang_filter, cfg.lang_filter, _exclude_inferred(cfg)),
  ).fetchall()
  return [
    ("item", row["id"], rank, float(row["distance"]))
    for rank, row in enumerate(rows)
  ]


def _vec_search_consolidations(
  conn: sqlite3.Connection,
  embedding: object,
  cfg: HybridQueryConfig,
) -> list[tuple[str, str, int, float]]:
  blob = _embedding_to_blob(embedding)
  rows = conn.execute(
    """
    SELECT consolidations_vec.consolidation_id AS id, distance
    FROM consolidations_vec
    JOIN consolidations ON consolidations.id = consolidations_vec.consolidation_id
    WHERE consolidations_vec.embedding MATCH ?
      AND k = ?
      AND (? = '' OR consolidations.source IN (SELECT value FROM json_each(?)))
      AND (? IS NULL OR consolidations.start_timestamp >= ?)
      AND (? IS NULL OR consolidations.end_timestamp <= ?)
      AND (? IS NULL OR EXISTS (
            SELECT 1 FROM items i, json_each(consolidations.raw_item_ids) j
            WHERE i.id = j.value AND i.lang = ?))
    ORDER BY distance
    """,
    (blob, cfg.per_index_k) + _vec_filter_params(cfg) + (cfg.lang_filter, cfg.lang_filter),
  ).fetchall()
  return [
    ("consolidation", row["id"], rank, float(row["distance"]))
    for rank, row in enumerate(rows)
  ]


def _apply_relevance_floor(
  hydrated: list[HybridResult], floor: float
) -> list[HybridResult]:
  """Drop results scoring below ``floor`` × the top score.

  Used before a timestamp sort so an occurrence query ("when did I first/last
  …") ranks by date *among results that are actually relevant*, not letting a
  weak tangential match win on recency alone. Never empties a non-empty set:
  the top scorer always clears its own threshold. ``floor <= 0`` is a no-op."""
  if floor <= 0 or not hydrated:
    return hydrated
  top = max(r.score for r in hydrated)
  if top <= 0:
    return hydrated
  threshold = top * floor
  return [r for r in hydrated if r.score >= threshold]


def _exclude_inferred(cfg: HybridQueryConfig) -> int:
  """1 when items with an inferred (fallback) timestamp must be excluded.

  Recency/occurrence sorts (asc/desc) rank purely by timestamp, so an undated
  note stamped with its import mtime would float to the top of "what's the
  latest" or sink to "when did this first happen". Relevance sort ignores the
  timestamp, so inferred items stay eligible there.
  """
  return 1 if cfg.sort != "relevance" else 0


def _fts_query(text: str, synonyms: dict[str, list[str]] | None = None) -> str:
  tokens = [t for t in text.replace('"', " ").split() if t]
  if not tokens:
    return ""
  if synonyms:
    tokens = expand_fts_tokens(tokens, synonyms)
  # Prefix-match longer stems for morphological recall (øvelse→øvelsen/øvelser)
  # while leaving short tokens exact — a 4-char stem like "funn" prefix-expands
  # into far too many candidates (funnet/funne/funnene) and dilutes exact hits.
  return " OR ".join(f'"{t}"*' if len(t) >= 5 else f'"{t}"' for t in tokens)


def _filter_params(match: str, cfg: HybridQueryConfig):
  source_json = json.dumps(cfg.source_filter or [])
  source_flag = "" if not cfg.source_filter else "filter"
  since_iso = ensure_utc(cfg.since).isoformat() if cfg.since else None
  until_iso = ensure_utc(cfg.until).isoformat() if cfg.until else None
  return (
    match,
    source_flag,
    source_json,
    since_iso,
    since_iso,
    until_iso,
    until_iso,
  )


def _vec_filter_params(cfg: HybridQueryConfig):
  source_json = json.dumps(cfg.source_filter or [])
  source_flag = "" if not cfg.source_filter else "filter"
  since_iso = ensure_utc(cfg.since).isoformat() if cfg.since else None
  until_iso = ensure_utc(cfg.until).isoformat() if cfg.until else None
  return (
    source_flag,
    source_json,
    since_iso,
    since_iso,
    until_iso,
    until_iso,
  )


def _embedding_to_blob(embedding: object) -> bytes:
  if hasattr(embedding, "astype") and hasattr(embedding, "tobytes"):
    return embedding.astype("float32").tobytes()  # type: ignore[attr-defined]
  if isinstance(embedding, bytes):
    return embedding
  return array("f", [float(v) for v in cast(Iterable[float], embedding)]).tobytes()

# thread_coherence_credit: additive RRF credit for an atomic item whose
# thread_id matches a consolidation that ranks in the current top-3, gated to
# items that also have fts_rank is not None (lexically on-topic).
# credit = OMEGA * top3_cons_rrf_score.  Sweep OMEGA in [0.10, 0.20].
_THREAD_COHERENCE_OMEGA = 0.15

_RANK_AGREEMENT_DELTA = 0.05


def _fuse(
  ranked_lists: Sequence[list[tuple[str, str, int, float]]],
  cfg: HybridQueryConfig,
) -> dict[tuple[str, str], ScoreComponents]:
  fused: dict[tuple[str, str], ScoreComponents] = {}
  for ranking in ranked_lists:
    for kind, identifier, rank, raw_score in ranking:
      key = (kind, identifier)
      comp = fused.setdefault(key, ScoreComponents())
      contribution = 1.0 / (cfg.rrf_k + rank + 1)
      if kind == "consolidation" and cfg.prefer_consolidations:
        contribution *= cfg.consolidation_boost
      comp.rrf_score += contribution
      _stash_component(comp, ranking is ranked_lists[0] or ranking is ranked_lists[1], rank, raw_score, kind == "item")
  # Post-loop pass: reward mutual top-of-both-index agreement.
  # Only fires when a doc is strongly ranked in BOTH FTS and vector (rank<=2
  # in each), the cleanest "both modalities are confident" signal that RRF
  # otherwise flattens.
  for comp in fused.values():
    if (
      comp.fts_rank is not None
      and comp.fts_rank <= 2
      and comp.vector_rank is not None
      and comp.vector_rank <= 2
    ):
      comp.rrf_score *= 1.0 + _RANK_AGREEMENT_DELTA
  return fused


def _stash_component(
  comp: ScoreComponents,
  is_fts: bool,
  rank: int,
  raw_score: float,
  is_item: bool,
) -> None:
  if is_fts:
    if comp.fts_rank is None or rank < comp.fts_rank:
      comp.fts_rank = rank
      comp.fts_score = raw_score
  else:
    if comp.vector_rank is None or rank < comp.vector_rank:
      comp.vector_rank = rank
      comp.vector_distance = raw_score


def _hydrate(
  conn: sqlite3.Connection,
  fused: dict[tuple[str, str], ScoreComponents],
  cfg: HybridQueryConfig,
  hydrate_cap: int | None = None,
) -> list[HybridResult]:
  if not fused:
    return []
  cap = hydrate_cap if hydrate_cap is not None else cfg.top_k * 2
  ordered = sorted(fused.items(), key=lambda kv: kv[1].rrf_score, reverse=True)

  # thread_coherence_credit: find thread_ids of top-3 consolidations, then
  # give a small additive credit to atomic items that (a) share that thread_id
  # and (b) are lexically present (fts_rank is not None), so only on-topic
  # thread members are lifted.
  if _THREAD_COHERENCE_OMEGA > 0.0:
    top3_cons_thread: dict[str, float] = {}  # thread_id -> cons rrf_score
    for (kind, identifier), comp in ordered[:3]:
      if kind == "consolidation":
        t_row = conn.execute(
          "SELECT thread_id FROM consolidations WHERE id = ?", (identifier,)
        ).fetchone()
        if t_row is not None:
          tid = t_row[0] if not hasattr(t_row, "keys") else t_row["thread_id"]
          if tid is not None:
            # Keep the highest-scoring cons rrf_score for this thread
            if comp.rrf_score > top3_cons_thread.get(tid, 0.0):
              top3_cons_thread[tid] = comp.rrf_score
    if top3_cons_thread:
      for (kind, identifier), comp in ordered:
        if kind == "item" and comp.fts_rank is not None:
          t_row = conn.execute(
            "SELECT thread_id FROM items WHERE id = ?", (identifier,)
          ).fetchone()
          if t_row is not None:
            tid = t_row[0] if not hasattr(t_row, "keys") else t_row["thread_id"]
            if tid is not None and tid in top3_cons_thread:
              comp.rrf_score += _THREAD_COHERENCE_OMEGA * top3_cons_thread[tid]
      # Re-sort after credit injection
      ordered = sorted(fused.items(), key=lambda kv: kv[1].rrf_score, reverse=True)

  results: list[HybridResult] = []
  for (kind, identifier), components in ordered[:cap]:
    if kind == "item":
      result = _hydrate_item(conn, identifier, components, cfg)
    else:
      result = _hydrate_consolidation(conn, identifier, components, cfg)
    if result is not None:
      results.append(result)
  return results


def _browse_window(
  conn: sqlite3.Connection,
  cfg: HybridQueryConfig,
  cap: int,
) -> list[HybridResult]:
  """List items + consolidations inside cfg's [since, until] window by time.

  The no-match fallback for time-windowed queries (see caller). Pure metadata
  scan, no FTS/vector — score is 0.0 since there is no relevance signal; the
  caller's timestamp sort orders them. Honors the same source/lang/inferred
  filters as the index searches so a browse never surfaces what a search would
  have hidden."""
  results: list[HybridResult] = []
  empty = ScoreComponents()
  if cfg.include_items:
    rows = conn.execute(
      """
      SELECT id FROM items
      WHERE (? = '' OR source IN (SELECT value FROM json_each(?)))
        AND (? IS NULL OR timestamp >= ?)
        AND (? IS NULL OR timestamp <= ?)
        AND (? IS NULL OR lang = ?)
        AND (? = 0 OR timestamp_inferred = 0)
        AND consolidated_into IS NULL
      ORDER BY timestamp DESC
      LIMIT ?
      """,
      _window_params(cfg) + (cfg.lang_filter, cfg.lang_filter, _exclude_inferred(cfg), cap),
    ).fetchall()
    for row in rows:
      r = _hydrate_item(conn, row["id"], empty, cfg)
      if r is not None:
        results.append(r)
  if cfg.include_consolidations:
    rows = conn.execute(
      """
      SELECT id FROM consolidations
      WHERE (? = '' OR source IN (SELECT value FROM json_each(?)))
        AND (? IS NULL OR start_timestamp >= ?)
        AND (? IS NULL OR end_timestamp <= ?)
      ORDER BY start_timestamp DESC
      LIMIT ?
      """,
      _window_params(cfg) + (cap,),
    ).fetchall()
    for row in rows:
      r = _hydrate_consolidation(conn, row["id"], empty, cfg)
      if r is not None:
        results.append(r)
  return results


def _window_params(cfg: HybridQueryConfig):
  source_json = json.dumps(cfg.source_filter or [])
  source_flag = "" if not cfg.source_filter else "filter"
  since_iso = ensure_utc(cfg.since).isoformat() if cfg.since else None
  until_iso = ensure_utc(cfg.until).isoformat() if cfg.until else None
  return (source_flag, source_json, since_iso, since_iso, until_iso, until_iso)


def _hydrate_item(
  conn: sqlite3.Connection,
  item_id: str,
  components: ScoreComponents,
  cfg: HybridQueryConfig,
) -> HybridResult | None:
  row = conn.execute(
    """
    SELECT id, source, timestamp, sender, recipients, content, subject, thread_id, raw_metadata
    FROM items WHERE id = ?
    """,
    (item_id,),
  ).fetchone()
  if row is None:
    return None
  if cfg.sender_filter and row["sender"] not in cfg.sender_filter:
    return None
  recipients = json.loads(row["recipients"] or "[]")
  score = components.rrf_score
  # tier2_factual_coverage_recovery: additive RRF credit for tier2 items that
  # are FTS-present but vector-absent on factual queries, BEFORE tier2_boost.
  if (
    cfg.tier2_factual_coverage_gamma > 0.0
    and cfg.query_shape == "factual"
    and row["source"] == cfg.tier2_source
    and components.fts_rank is not None
    and components.fts_rank <= 5
    and components.vector_rank is None
  ):
    score += cfg.tier2_factual_coverage_gamma / (cfg.rrf_k + components.fts_rank + 1)
  if cfg.tier2_boost != 1.0 and row["source"] == cfg.tier2_source:
    score *= cfg.tier2_boost
  return HybridResult(
    id=row["id"],
    kind="item",
    source=row["source"],
    timestamp=ensure_utc(_parse_iso(row["timestamp"])),
    sender=row["sender"] or "",
    subject=row["subject"] or "",
    content=row["content"] or "",
    thread_id=row["thread_id"],
    score=score,
    components=components,
    participants=recipients,
  )


def _hydrate_consolidation(
  conn: sqlite3.Connection,
  cid: str,
  components: ScoreComponents,
  cfg: HybridQueryConfig,
) -> HybridResult | None:
  row = conn.execute(
    """
    SELECT id, source, thread_id, start_timestamp, participants, item_count, summary
    FROM consolidations WHERE id = ?
    """,
    (cid,),
  ).fetchone()
  if row is None:
    return None
  participants = json.loads(row["participants"] or "[]")
  if cfg.sender_filter and not any(p in cfg.sender_filter for p in participants):
    return None
  return HybridResult(
    id=row["id"],
    kind="consolidation",
    source=row["source"],
    timestamp=ensure_utc(_parse_iso(row["start_timestamp"])),
    sender=", ".join(participants[:3]),
    subject=f"{row['source']} session ({row['item_count']} items)",
    content=row["summary"] or "",
    thread_id=row["thread_id"],
    score=components.rrf_score,
    components=components,
    participants=participants,
    item_count=int(row["item_count"]),
  )


def _parse_iso(value: str) -> datetime:
  return datetime.fromisoformat(value.replace("Z", "+00:00"))
