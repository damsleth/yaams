from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from yaams.ingest.base import Item, hash_id
from yaams.retrieve import HybridQueryConfig, ParsedQuery, filter_results_by_entities
from yaams.retrieve.route import route
from yaams.schema import init_schema
from yaams.store import store_items


def _parsed(**overrides) -> ParsedQuery:
  base = dict(
    raw="q",
    shape="factual",
    entities=[],
    date_range=(None, None),
    topic_terms=[],
    sort="relevance",
    prefer_tier=None,
    high_quality=False,
    fallback_used=False,
  )
  base.update(overrides)
  return ParsedQuery(**base)


def test_route_factual_is_passthrough():
  base = HybridQueryConfig(top_k=10)
  cfg = route(_parsed(), base)
  assert cfg.top_k == 10
  assert cfg.sort == "relevance"
  assert cfg.high_quality is False
  assert cfg.entity_filter is None


def test_route_temporal_narrow_window_deboosts_consolidations():
  # A single-day query wants that day's item, not the broad session rollup.
  base = HybridQueryConfig(top_k=10)
  day = datetime(2026, 4, 4, tzinfo=UTC)
  cfg = route(_parsed(shape="temporal_range", date_range=(day, day)), base)
  assert cfg.consolidation_boost < 1.0


def test_route_temporal_wide_window_keeps_consolidation_boost():
  # A month-wide "activity in May" query is a summary — keep consolidations up.
  base = HybridQueryConfig(top_k=10)
  cfg = route(
    _parsed(
      shape="temporal_range",
      date_range=(datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)),
    ),
    base,
  )
  assert cfg.consolidation_boost == base.consolidation_boost


def test_route_synthesis_bumps_top_k_and_high_quality():
  base = HybridQueryConfig(top_k=5)
  cfg = route(_parsed(shape="synthesis"), base)
  assert cfg.top_k >= 12
  assert cfg.high_quality is True
  assert cfg.prefer_consolidations is True


def test_route_first_occurrence_sets_asc_sort():
  base = HybridQueryConfig()
  cfg = route(_parsed(shape="first_occurrence"), base)
  assert cfg.sort == "asc"


def test_route_last_occurrence_sets_desc_sort():
  base = HybridQueryConfig()
  cfg = route(_parsed(shape="last_occurrence", sort="desc"), base)
  assert cfg.sort == "desc"


def test_route_explicit_sort_wins_over_shape_inference():
  # User asked for oldest-first; a last_occurrence shape must not flip it.
  base = HybridQueryConfig(sort="asc")
  cfg = route(_parsed(shape="last_occurrence", sort="desc"), base, explicit_sort=True)
  assert cfg.sort == "asc"


def test_route_explicit_relevance_blocks_parsed_sort():
  # --sort relevance is authoritative even when the parser inferred desc.
  base = HybridQueryConfig(sort="relevance")
  cfg = route(_parsed(shape="last_occurrence", sort="desc"), base, explicit_sort=True)
  assert cfg.sort == "relevance"


def test_route_event_anchored_boosts_consolidations():
  base = HybridQueryConfig(top_k=20)
  cfg = route(_parsed(shape="event_anchored"), base)
  assert cfg.consolidation_boost >= 1.3
  assert cfg.top_k <= 8


def test_route_tier2_preference_bumps_boost():
  base = HybridQueryConfig(tier2_boost=1.2)
  cfg = route(_parsed(prefer_tier="tier2_ledger"), base)
  assert cfg.tier2_boost >= 1.6


def test_route_raw_preference_drops_tier2_boost():
  base = HybridQueryConfig(tier2_boost=1.5)
  cfg = route(_parsed(prefer_tier="raw"), base)
  assert cfg.tier2_boost == 1.0


def test_route_explicit_user_since_wins_over_parsed():
  user_since = datetime(2025, 1, 1, tzinfo=UTC)
  parsed_since = datetime(2026, 4, 1, tzinfo=UTC)
  base = HybridQueryConfig(since=user_since)
  cfg = route(
    _parsed(date_range=(parsed_since, None)),
    base,
    explicit_since=True,
  )
  assert cfg.since == user_since


def test_route_parsed_date_range_applied_when_no_explicit_flag():
  start = datetime(2026, 4, 1, tzinfo=UTC)
  end = datetime(2026, 4, 30, tzinfo=UTC)
  cfg = route(_parsed(date_range=(start, end)), HybridQueryConfig())
  assert cfg.since == start
  assert cfg.until == end


def test_route_sets_entity_filter_from_parsed_entities():
  cfg = route(_parsed(entities=["Bob Smith"]), HybridQueryConfig())
  assert cfg.entity_filter == ["Bob Smith"]


def test_route_intent_terms_demote_entity_filter_to_boost():
  # #3: a relevance-ranked query with topic terms lifts entity docs via a soft
  # boost instead of hard-filtering, so the intent term can outrank entity noise.
  cfg = route(
    _parsed(shape="factual", entities=["M365", "IAM"], topic_terms=["incidents"]),
    HybridQueryConfig(),
  )
  assert cfg.entity_filter is None
  assert cfg.boost_entities == ["M365", "IAM"]


def test_route_occurrence_with_topic_terms_keeps_hard_entity_filter():
  # A soft boost is invisible to a timestamp sort, so occurrence shapes must
  # keep the hard entity filter even when intent terms are present — otherwise
  # the entity constraint vanishes and a tangential early/late item wins.
  for shape in ("first_occurrence", "last_occurrence"):
    cfg = route(
      _parsed(shape=shape, entities=["NOCOS"], topic_terms=["involved"]),
      HybridQueryConfig(),
    )
    assert cfg.entity_filter == ["NOCOS"], shape
    assert cfg.boost_entities is None, shape


def test_route_synthesis_keeps_hard_entity_filter_with_topic_terms():
  # Synthesis needs clean scoping even when topic terms are present.
  cfg = route(
    _parsed(shape="synthesis", entities=["NOCOS"], topic_terms=["provisioning"]),
    HybridQueryConfig(),
  )
  assert cfg.entity_filter == ["NOCOS"]
  assert cfg.boost_entities is None


def test_route_entities_without_topic_terms_stay_hard_filter():
  # No intent terms → the entity is the only signal; keep the hard filter.
  cfg = route(_parsed(shape="factual", entities=["Bob Smith"]), HybridQueryConfig())
  assert cfg.entity_filter == ["Bob Smith"]
  assert cfg.boost_entities is None


def test_route_occurrence_sets_participant_filter_from_identities():
  # #2: first/last_occurrence anchors on the user's participation.
  ids = ["me", "cdam@une.no"]
  first = route(_parsed(shape="first_occurrence"), HybridQueryConfig(), self_identities=ids)
  last = route(_parsed(shape="last_occurrence"), HybridQueryConfig(), self_identities=ids)
  assert first.participant_filter == ids
  assert last.participant_filter == ids


def test_route_no_participant_filter_without_identities():
  cfg = route(_parsed(shape="first_occurrence"), HybridQueryConfig())
  assert cfg.participant_filter is None


def test_route_occurrence_sets_relevance_floor():
  first = route(_parsed(shape="first_occurrence"), HybridQueryConfig())
  last = route(_parsed(shape="last_occurrence"), HybridQueryConfig())
  assert first.relevance_floor > 0
  assert last.relevance_floor > 0


def test_route_explicit_sort_does_not_set_relevance_floor():
  # User --sort wins over shape and must not get the occurrence floor.
  cfg = route(
    _parsed(shape="last_occurrence"), HybridQueryConfig(sort="desc"), explicit_sort=True
  )
  assert cfg.relevance_floor == 0.0


def test_route_factual_has_no_relevance_floor():
  cfg = route(_parsed(shape="factual"), HybridQueryConfig())
  assert cfg.relevance_floor == 0.0


def test_route_synthesis_does_not_set_participant_filter():
  cfg = route(_parsed(shape="synthesis"), HybridQueryConfig(), self_identities=["me"])
  assert cfg.participant_filter is None


def test_filter_results_by_entities_drops_unmatched_items():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
    ("Bob Smith", "person"),
  )
  ent_id = conn.execute(
    "SELECT id FROM entities WHERE canonical_name = ?", ("Bob Smith",)
  ).fetchone()["id"]

  matching = Item(
    id=hash_id("imessage", "thread:1"),
    source="imessage",
    source_id="thread:1",
    timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    sender="alice",
    recipients=[],
    content="bob said hi",
    subject="",
    thread_id="thread",
  )
  other = Item(
    id=hash_id("imessage", "thread:2"),
    source="imessage",
    source_id="thread:2",
    timestamp=datetime(2026, 4, 1, 12, 1, tzinfo=UTC),
    sender="alice",
    recipients=[],
    content="orphan content",
    subject="",
    thread_id="thread",
  )
  store_items(conn, [matching, other], [b"\x00" * 16] * 2, [[]] * 2)
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, ?)",
    (matching.id, ent_id, "test"),
  )
  conn.commit()

  from yaams.retrieve import HybridResult, ScoreComponents

  results = [
    HybridResult(
      id=matching.id,
      kind="item",
      source="imessage",
      timestamp=matching.timestamp,
      sender="alice",
      subject="",
      content=matching.content,
      thread_id="thread",
      score=1.0,
      components=ScoreComponents(),
    ),
    HybridResult(
      id=other.id,
      kind="item",
      source="imessage",
      timestamp=other.timestamp,
      sender="alice",
      subject="",
      content=other.content,
      thread_id="thread",
      score=0.5,
      components=ScoreComponents(),
    ),
  ]

  filtered = filter_results_by_entities(results, conn, ["Bob Smith"])
  assert [r.id for r in filtered] == [matching.id]


def test_filter_results_by_entities_passthrough_when_empty():
  results: list = ["sentinel"]
  assert filter_results_by_entities(results, None, None) == ["sentinel"]
