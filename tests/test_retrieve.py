from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from yaams.consolidate import build_consolidations
from yaams.ingest.base import Item, hash_id
from yaams.retrieve import HybridQueryConfig, query
from yaams.schema import init_schema
from yaams.store import store_consolidations, store_items


def _make_item(
  source: str = "imessage",
  thread_id: str = "thread-1",
  sender: str = "alice@example.test",
  content: str = "hello world",
  ts: datetime | None = None,
  msg_id: str = "1",
) -> Item:
  return Item(
    id=hash_id(source, f"{thread_id}:{msg_id}"),
    source=source,
    source_id=f"{thread_id}:{msg_id}",
    timestamp=ts or datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    sender=sender,
    recipients=[],
    content=content,
    subject="",
    thread_id=thread_id,
  )


def _open_db():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _zero_embedding():
  return [0.0, 0.0, 0.0, 0.0]


def test_query_empty_text_returns_nothing():
  conn = _open_db()
  results = query(conn, "")
  assert results == []


def test_fts_only_returns_matching_items():
  conn = _open_db()
  items = [
    _make_item(content="apples and oranges", msg_id="1"),
    _make_item(content="bananas and grapes", msg_id="2"),
    _make_item(content="totally unrelated text", msg_id="3"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  results = query(conn, "apples")
  assert len(results) >= 1
  assert any("apple" in r.content.lower() for r in results)


def test_query_filters_by_source():
  conn = _open_db()
  items = [
    _make_item(source="imessage", content="kim has a dog", msg_id="1"),
    _make_item(source="teams_work", content="kim has a cat", msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(source_filter=["teams_work"])
  results = query(conn, "kim", config=cfg)
  assert all(r.source == "teams_work" for r in results)
  assert len(results) >= 1


def test_query_excludes_consolidated_items_by_default():
  conn = _open_db()
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    _make_item(content=f"keyword foo bar {i}", ts=base + timedelta(minutes=i), msg_id=f"m{i}")
    for i in range(5)
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  consolidations = build_consolidations(items)
  assert len(consolidations) == 1
  store_consolidations(conn, consolidations, embeddings=[b"\x00" * 16])

  results = query(conn, "keyword foo")
  kinds = {r.kind for r in results}
  assert "consolidation" in kinds
  for r in results:
    if r.kind == "item":
      pytest.fail(f"raw item leaked through after consolidation: {r.id}")


def test_no_vector_mode_skips_dense_search():
  conn = _open_db()
  items = [
    _make_item(content="quick brown fox", msg_id="1"),
    _make_item(content="lazy dog sleeps", msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))
  results = query(conn, "fox")
  assert results
  assert all(r.components.vector_rank is None for r in results)


def test_consolidation_boost_lifts_consolidations_above_raw_items():
  conn = _open_db()
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    _make_item(
      thread_id="t1",
      content=f"unique-token-xyz session {i}",
      ts=base + timedelta(minutes=i),
      msg_id=f"m{i}",
    )
    for i in range(5)
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))
  cons = build_consolidations(items)
  store_consolidations(conn, cons, embeddings=[b"\x00" * 16])

  other = _make_item(
    thread_id="t2",
    content="unique-token-xyz orphan",
    msg_id="m99",
  )
  store_items(conn, [other], [b"\x00" * 16], [[]])

  results = query(conn, "unique-token-xyz")
  assert results
  assert results[0].kind == "consolidation"


def test_tier2_boost_promotes_ledger_above_raw_for_equal_match():
  conn = _open_db()
  raw = _make_item(
    source="email",
    thread_id="t-raw",
    content="alpha tier2 distinguishing token",
    msg_id="raw-1",
  )
  ledger = _make_item(
    source="tier2_ledger",
    thread_id="t-ledger",
    content="alpha tier2 distinguishing token",
    msg_id="ledger-1",
  )
  store_items(conn, [raw, ledger], [b"\x00" * 16] * 2, [[]] * 2)

  baseline = HybridQueryConfig(tier2_boost=1.0, include_consolidations=False)
  baseline_results = query(conn, "alpha tier2 distinguishing", config=baseline)
  baseline_sources = [r.source for r in baseline_results]
  assert "tier2_ledger" in baseline_sources
  assert "email" in baseline_sources

  boosted = HybridQueryConfig(tier2_boost=2.0, include_consolidations=False)
  boosted_results = query(conn, "alpha tier2 distinguishing", config=boosted)
  assert boosted_results
  assert boosted_results[0].source == "tier2_ledger"


def test_sort_asc_orders_results_by_timestamp():
  conn = _open_db()
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    _make_item(content="alpha-token first", ts=base, msg_id="1"),
    _make_item(content="alpha-token middle", ts=base + timedelta(hours=1), msg_id="2"),
    _make_item(content="alpha-token last", ts=base + timedelta(hours=2), msg_id="3"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(sort="asc", include_consolidations=False)
  results = query(conn, "alpha-token", config=cfg)
  assert len(results) >= 2
  for a, b in zip(results, results[1:]):
    assert a.timestamp <= b.timestamp


def test_sort_desc_orders_results_by_timestamp_desc():
  conn = _open_db()
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    _make_item(content="beta-token first", ts=base, msg_id="1"),
    _make_item(content="beta-token last", ts=base + timedelta(hours=2), msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(sort="desc", include_consolidations=False)
  results = query(conn, "beta-token", config=cfg)
  assert len(results) >= 2
  for a, b in zip(results, results[1:]):
    assert a.timestamp >= b.timestamp


def test_first_occurrence_picks_oldest_match_outside_relevance_window():
  conn = _open_db()
  base = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
  items = []
  # Many strong matches near the present (would dominate relevance top-k)
  for i in range(15):
    items.append(
      _make_item(
        thread_id="recent",
        content="phoenix project meeting recurring",
        ts=datetime(2026, 4, 1, tzinfo=UTC) + timedelta(minutes=i),
        msg_id=f"recent-{i}",
      )
    )
  # One older, weaker match - should win for first_occurrence
  items.append(
    _make_item(
      thread_id="old",
      content="phoenix project first mention only",
      ts=base,
      msg_id="old-1",
    )
  )
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(top_k=5, sort="asc", include_consolidations=False)
  results = query(conn, "phoenix project", config=cfg)
  assert results, "expected at least one result for first_occurrence"
  assert results[0].timestamp == base, (
    f"expected oldest match at top, got {results[0].timestamp}"
  )


def test_entity_filter_includes_match_below_relevance_top_k():
  from yaams.retrieve import filter_results_by_entities

  conn = _open_db()
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
    ("Bob Smith", "person"),
  )
  ent_id = conn.execute(
    "SELECT id FROM entities WHERE canonical_name = ?", ("Bob Smith",)
  ).fetchone()["id"]

  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = []
  # Many strong, recent ATLAS items NOT linked to Bob
  for i in range(20):
    items.append(
      _make_item(
        thread_id=f"unrelated-{i}",
        content="ATLAS provisioning rollout chatter",
        ts=base + timedelta(minutes=i),
        msg_id=f"u-{i}",
      )
    )
  # One weaker ATLAS item linked to Bob (the one we want)
  target = _make_item(
    thread_id="target",
    content="ATLAS",
    ts=base + timedelta(hours=1),
    msg_id="target-1",
  )
  items.append(target)
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, ?)",
    (target.id, ent_id, "test"),
  )
  conn.commit()

  cfg = HybridQueryConfig(
    top_k=5,
    per_index_k=10,
    include_consolidations=False,
    entity_filter=["Bob Smith"],
  )
  results = query(conn, "ATLAS", config=cfg)
  ids = {r.id for r in results}
  assert target.id in ids, "entity-matched item must survive even outside top relevance"
  # Belt-and-suspenders: post-filter agrees
  filtered = filter_results_by_entities(results, conn, ["Bob Smith"])
  assert all(r.id == target.id for r in filtered)


def test_entity_filter_drops_unrelated_consolidation():
  from yaams.retrieve import filter_results_by_entities

  conn = _open_db()
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
    ("Bob Smith", "person"),
  )
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    _make_item(
      thread_id="unrelated",
      content=f"ATLAS chatter {i}",
      ts=base + timedelta(minutes=i),
      msg_id=f"u-{i}",
    )
    for i in range(5)
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))
  cons = build_consolidations(items)
  store_consolidations(conn, cons, embeddings=[b"\x00" * 16])
  conn.commit()

  cfg = HybridQueryConfig(
    top_k=5,
    entity_filter=["Bob Smith"],
  )
  results = query(conn, "ATLAS", config=cfg)
  assert all(r.kind != "consolidation" for r in results), (
    "consolidation with no entity-linked raw items leaked through entity filter"
  )
  # Post-filter agrees
  filtered = filter_results_by_entities(results, conn, ["Bob Smith"])
  assert filtered == []


def test_high_quality_increases_per_index_fetch():
  conn = _open_db()
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    _make_item(
      thread_id=f"t-{i}",
      content=f"shared-token doc {i}",
      ts=base + timedelta(minutes=i),
      msg_id=f"m-{i}",
    )
    for i in range(80)
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  baseline = HybridQueryConfig(
    top_k=20, per_index_k=30, include_consolidations=False
  )
  baseline_results = query(conn, "shared-token", config=baseline)
  hq = HybridQueryConfig(
    top_k=20, per_index_k=30, include_consolidations=False, high_quality=True
  )
  hq_results = query(conn, "shared-token", config=hq)
  # high_quality should never return fewer candidates than baseline
  assert len(hq_results) >= len(baseline_results)
  # At top_k=20 with 80 candidates, high_quality fetch (>=60) should fill
  assert len(hq_results) == 20


def test_synonym_expansion_reaches_alias_only_document():
  import json as _json

  conn = _open_db()
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases) VALUES (?, ?, ?)",
    ("Norconsult", "org", _json.dumps(["nc", "NC"])),
  )
  conn.commit()
  items = [
    _make_item(content="Norconsult signed the new framework agreement", msg_id="1"),
    _make_item(content="totally unrelated lunch plans", msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  # Query the short alias; expansion should OR in "Norconsult" and hit doc 1.
  results = query(conn, "nc", config=HybridQueryConfig(include_consolidations=False))
  assert any("Norconsult" in r.content for r in results), (
    "synonym expansion should let 'nc' reach the Norconsult document"
  )


def test_synonym_expansion_disabled_misses_alias_only_document():
  import json as _json

  conn = _open_db()
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases) VALUES (?, ?, ?)",
    ("Norconsult", "org", _json.dumps(["nc", "NC"])),
  )
  conn.commit()
  items = [_make_item(content="Norconsult signed the agreement", msg_id="1")]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(include_consolidations=False, expand_synonyms=False)
  results = query(conn, "nc", config=cfg)
  assert not any("Norconsult" in r.content for r in results), (
    "with expansion off, literal FTS for 'nc' must not reach 'Norconsult'"
  )


def test_association_boost_surfaces_associated_doc_below_exact_match():
  from yaams.retrieve.associate import expand_query_entities

  conn = _open_db()
  conn.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('fdep','org')")
  conn.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('langkaia','place')")
  fdep = conn.execute("SELECT id FROM entities WHERE canonical_name='fdep'").fetchone()["id"]
  langkaia = conn.execute(
    "SELECT id FROM entities WHERE canonical_name='langkaia'"
  ).fetchone()["id"]
  # Hand-authored association: fdep located_at langkaia.
  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, weight, suppress) "
    "VALUES (?, ?, 0.6, 0)",
    (fdep, langkaia),
  )

  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  exact = _make_item(thread_id="exact", content="shared-topic briefing", ts=base, msg_id="1")
  related = _make_item(
    thread_id="related", content="shared-topic briefing", ts=base + timedelta(hours=1), msg_id="2"
  )
  store_items(conn, [exact, related], [b"\x00" * 16] * 2, [[]] * 2)
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'test')",
    (exact.id, fdep),
  )
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'test')",
    (related.id, langkaia),
  )
  conn.commit()

  # Hard entity filter alone: only the fdep doc survives.
  strict = HybridQueryConfig(entity_filter=["fdep"], include_consolidations=False)
  strict_ids = {r.id for r in query(conn, "shared-topic", config=strict)}
  assert strict_ids == {exact.id}

  # With association expansion: the langkaia doc surfaces but ranks below fdep.
  expanded, weights = expand_query_entities(conn, ["fdep"])
  assoc_cfg = HybridQueryConfig(
    entity_filter=expanded, assoc_weights=weights, include_consolidations=False
  )
  results = query(conn, "shared-topic", config=assoc_cfg)
  ids = [r.id for r in results]
  assert exact.id in ids and related.id in ids
  assert ids[0] == exact.id, "exact entity match must outrank the associated doc"


def test_score_components_record_fts_rank():
  conn = _open_db()
  items = [
    _make_item(content="alpha beta gamma", msg_id="1"),
    _make_item(content="delta epsilon", msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  results = query(conn, "alpha")
  assert results
  top = results[0]
  assert top.components.fts_rank == 0
  assert top.components.fts_score is not None
  assert top.components.rrf_score > 0
