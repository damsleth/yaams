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
    content="theodor said hi",
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
