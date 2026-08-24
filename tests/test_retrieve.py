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
  recipients: list[str] | None = None,
  timestamp_inferred: bool = False,
) -> Item:
  return Item(
    id=hash_id(source, f"{thread_id}:{msg_id}"),
    source=source,
    source_id=f"{thread_id}:{msg_id}",
    timestamp=ts or datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    sender=sender,
    recipients=recipients or [],
    content=content,
    subject="",
    thread_id=thread_id,
    timestamp_inferred=timestamp_inferred,
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


def test_browse_window_fallback_when_text_matches_nothing():
  # A time-windowed query whose text matches no item should fall back to
  # listing the items inside the window, not return zero.
  conn = _open_db()
  base = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
  items = [
    _make_item(content="standup notes alpha", ts=base, msg_id="in1"),
    _make_item(content="lunch plans beta", ts=base + timedelta(hours=2), msg_id="in2"),
    _make_item(content="outside the window", ts=base + timedelta(days=10), msg_id="out"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(
    since=datetime(2026, 5, 14, 0, 0, tzinfo=UTC),
    until=datetime(2026, 5, 14, 23, 59, tzinfo=UTC),
    sort="desc",
  )
  results = query(conn, "zzz nonmatching gibberish", config=cfg)
  assert len(results) == 2  # only the two in-window items, not the out-of-window one
  assert all(r.timestamp.date() == base.date() for r in results)


def test_browse_window_skipped_when_entity_filter_set():
  # Entity-filtered queries must NOT dump the whole window when nothing matches.
  conn = _open_db()
  items = [_make_item(content="something", ts=datetime(2026, 5, 14, 9, 0, tzinfo=UTC))]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))
  cfg = HybridQueryConfig(
    since=datetime(2026, 5, 14, 0, 0, tzinfo=UTC),
    until=datetime(2026, 5, 14, 23, 59, tzinfo=UTC),
    entity_filter=["nonexistent_entity"],
  )
  assert query(conn, "zzz nonmatching", config=cfg) == []


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


def test_feedback_boost_promotes_cited_result():
  from yaams.signals import log_query

  conn = _open_db()
  a = _make_item(thread_id="t-a", content="zeta distinctive shared token", msg_id="a-1")
  b = _make_item(thread_id="t-b", content="zeta distinctive shared token", msg_id="b-1")
  store_items(conn, [a, b], [b"\x00" * 16] * 2, [[]] * 2)

  base = HybridQueryConfig(include_consolidations=False)
  baseline = query(conn, "zeta distinctive shared", config=base)
  assert len(baseline) == 2
  underdog = baseline[-1]  # currently ranked last on FTS alone

  # Six logged queries cited the underdog -> automatic positive signal, boost
  # saturates the cap and must flip it above the incumbent.
  for i in range(6):
    log_query(
      conn, query_id=f"q_boost_{i}", text="zeta distinctive shared", top_k=2,
      source_filter=None, since=None, until=None, results=baseline,
      cited_result_ids=[underdog.id],
    )

  boosted = query(
    conn, "zeta distinctive shared",
    config=HybridQueryConfig(include_consolidations=False, feedback_boost=True),
  )
  assert boosted[0].id == underdog.id

  # Off by default: no reordering without the flag.
  unboosted = query(conn, "zeta distinctive shared", config=base)
  assert unboosted[0].id == baseline[0].id


def test_feedback_boost_correction_promotes_corrected_result():
  from yaams.signals import log_feedback, log_query

  conn = _open_db()
  a = _make_item(thread_id="t-a", content="omega distinctive shared token", msg_id="a-1")
  b = _make_item(thread_id="t-b", content="omega distinctive shared token", msg_id="b-1")
  store_items(conn, [a, b], [b"\x00" * 16] * 2, [[]] * 2)

  base = HybridQueryConfig(include_consolidations=False)
  baseline = query(conn, "omega distinctive shared", config=base)
  underdog = baseline[-1]  # ranked last on FTS alone

  # A correction names the underdog as the RIGHT (mis-ranked) answer -> positive
  # signal that must lift it, not demote it (the sign bug this guards against).
  for i in range(6):
    log_query(
      conn, query_id=f"q_corr_{i}", text="omega distinctive shared", top_k=2,
      source_filter=None, since=None, until=None, results=baseline,
    )
    log_feedback(conn, query_id=f"q_corr_{i}", kind="correction", result_id=underdog.id)

  promoted = query(
    conn, "omega distinctive shared",
    config=HybridQueryConfig(include_consolidations=False, feedback_boost=True),
  )
  assert promoted[0].id == underdog.id


def test_feedback_boost_leave_one_out_suppresses_self_signal():
  from yaams.signals import log_query

  conn = _open_db()
  a = _make_item(thread_id="t-a", content="kappa distinctive shared token", msg_id="a-1")
  b = _make_item(thread_id="t-b", content="kappa distinctive shared token", msg_id="b-1")
  store_items(conn, [a, b], [b"\x00" * 16] * 2, [[]] * 2)

  base = HybridQueryConfig(include_consolidations=False)
  baseline = query(conn, "kappa distinctive shared", config=base)
  underdog = baseline[-1]

  # Exactly two citations: enough to flip the underdog to rank 1 (each +3%,
  # clearing the RRF rank-0/rank-1 gap). One comes from q_self.
  for qid in ("q_self", "q_other"):
    log_query(
      conn, query_id=qid, text="kappa distinctive shared", top_k=2,
      source_filter=None, since=None, until=None, results=baseline,
      cited_result_ids=[underdog.id],
    )

  boost_cfg = HybridQueryConfig(include_consolidations=False, feedback_boost=True)
  assert query(conn, "kappa distinctive shared", config=boost_cfg)[0].id == underdog.id

  # Leave-one-out on q_self drops to a single citation (+3%), below the flip
  # threshold, so the original order is restored — eval never boosts from itself.
  loo_cfg = HybridQueryConfig(
    include_consolidations=False, feedback_boost=True,
    feedback_boost_exclude_query_id="q_self",
  )
  assert query(conn, "kappa distinctive shared", config=loo_cfg)[0].id == baseline[0].id


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


def test_recency_sort_excludes_inferred_timestamps():
  # #1: an undated note stamped with a (recent) import mtime must not float to
  # the top of a desc/recency sort. Relevance sort still includes it.
  conn = _open_db()
  base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
  real = _make_item(
    source="teams_work", content="gamma-token real", ts=base, msg_id="1"
  )
  undated = _make_item(
    source="notes",
    content="gamma-token undated",
    ts=base + timedelta(days=200),  # newer, but inferred
    msg_id="2",
    timestamp_inferred=True,
  )
  store_items(conn, [real, undated], [b"\x00" * 16] * 2, [[]] * 2)

  desc = query(conn, "gamma-token", config=HybridQueryConfig(sort="desc", include_consolidations=False))
  assert [r.id for r in desc] == [real.id]  # inferred excluded despite being newer

  rel = query(conn, "gamma-token", config=HybridQueryConfig(sort="relevance", include_consolidations=False))
  assert undated.id in {r.id for r in rel}  # still eligible on relevance


def test_participant_filter_restricts_to_user_activity():
  # #2: only items the user sent or received survive the participant filter.
  conn = _open_db()
  mine_sender = _make_item(
    source="teams_work", sender="cdam@une.no", content="delta-token a", msg_id="1"
  )
  mine_recipient = _make_item(
    source="teams_work",
    sender="someone@else.test",
    recipients=["Damsleth, Carl Joakim"],
    content="delta-token b",
    msg_id="2",
  )
  theirs = _make_item(
    source="teams_work", sender="other@else.test", content="delta-token c", msg_id="3"
  )
  store_items(conn, [mine_sender, mine_recipient, theirs], [b"\x00" * 16] * 3, [[]] * 3)

  cfg = HybridQueryConfig(
    participant_filter=["cdam@une.no", "Damsleth, Carl Joakim"],
    include_consolidations=False,
  )
  results = query(conn, "delta-token", config=cfg)
  ids = {r.id for r in results}
  assert mine_sender.id in ids
  assert mine_recipient.id in ids
  assert theirs.id not in ids


def test_relevance_floor_drops_weak_tail_before_timestamp_sort():
  from yaams.retrieve.hybrid import HybridResult, _apply_relevance_floor

  def _r(rid: str, score: float) -> HybridResult:
    return HybridResult(
      id=rid, kind="item", source="notes",
      timestamp=datetime(2026, 1, 1, tzinfo=UTC),
      sender="me", subject="", content="", thread_id=None, score=score,
    )

  results = [_r("strong", 1.0), _r("mid", 0.5), _r("weak", 0.1)]
  kept = {r.id for r in _apply_relevance_floor(results, 0.2)}
  assert kept == {"strong", "mid"}  # weak (0.1 < 0.2*1.0) dropped

  # floor 0 is a no-op; the top scorer always clears its own threshold.
  assert len(_apply_relevance_floor(results, 0.0)) == 3
  assert len(_apply_relevance_floor([_r("only", 0.4)], 0.9)) == 1


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


def test_configured_synonym_expansion_reaches_cross_lingual_document():
  conn = _open_db()
  items = [
    _make_item(content="Ops shift handoff covered the deploy window", msg_id="1"),
    _make_item(content="totally unrelated lunch plans", msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(
    include_consolidations=False,
    synonym_groups=[["vakt", "shift"]],
  )
  results = query(conn, "vakt", config=cfg)

  assert any("shift handoff" in r.content for r in results)


def test_fts_prefix_expansion_reaches_norwegian_inflection():
  conn = _open_db()
  items = [
    _make_item(content="Planen for øvelsen ble avklart i møtet", msg_id="1"),
    _make_item(content="totally unrelated lunch plans", msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  results = query(
    conn,
    "øvelse",
    config=HybridQueryConfig(include_consolidations=False),
  )

  assert any("øvelsen" in r.content for r in results)


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


def test_association_exact_outranks_newer_associated_under_date_sort():
  # Regression: a score multiply + (timestamp, -score) sort let a NEWER
  # associated-only doc beat an older exact match. The exact-before-associated
  # partition must hold even when the associated doc is more recent.
  from yaams.retrieve.associate import expand_query_entities

  conn = _open_db()
  conn.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('fdep','org')")
  conn.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('langkaia','place')")
  fdep = conn.execute("SELECT id FROM entities WHERE canonical_name='fdep'").fetchone()["id"]
  langkaia = conn.execute(
    "SELECT id FROM entities WHERE canonical_name='langkaia'"
  ).fetchone()["id"]
  conn.execute(
    "INSERT INTO entity_relations (from_entity, to_entity, weight, suppress) "
    "VALUES (?, ?, 0.6, 0)",
    (fdep, langkaia),
  )

  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  exact = _make_item(thread_id="exact", content="shared-topic", ts=base, msg_id="1")
  newer_related = _make_item(
    thread_id="related", content="shared-topic", ts=base + timedelta(days=10), msg_id="2"
  )
  store_items(conn, [exact, newer_related], [b"\x00" * 16] * 2, [[]] * 2)
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'test')",
    (exact.id, fdep),
  )
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'test')",
    (newer_related.id, langkaia),
  )
  conn.commit()

  expanded, weights = expand_query_entities(conn, ["fdep"])
  cfg = HybridQueryConfig(
    entity_filter=expanded, assoc_weights=weights, sort="desc",
    include_consolidations=False,
  )
  results = query(conn, "shared-topic", config=cfg)
  ids = [r.id for r in results]
  assert newer_related.id in ids
  assert ids[0] == exact.id, (
    "exact entity match must rank above a newer associated-only doc"
  )


def test_metadata_boost_lifts_tagged_entity_without_filtering():
  from yaams.store import add_entity_tags, resolve_entity_id

  conn = _open_db()
  conn.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('Acme','org')")
  acme = resolve_entity_id(conn, "Acme")
  add_entity_tags(conn, acme, ["customer"])

  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  tagged = _make_item(thread_id="t1", content="shared-topic", ts=base, msg_id="1")
  untagged = _make_item(
    thread_id="t2", content="shared-topic shared-topic", ts=base, msg_id="2"
  )  # stronger raw match
  store_items(conn, [tagged, untagged], [b"\x00" * 16] * 2, [[]] * 2)
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'test')",
    (tagged.id, acme),
  )
  conn.commit()

  # Boost mode keeps BOTH docs but lifts the customer-tagged one.
  matched = ["Acme"]
  boosted = HybridQueryConfig(
    boost_entities=matched, boost_factor=5.0, include_consolidations=False
  )
  results = query(conn, "shared-topic", config=boosted)
  ids = [r.id for r in results]
  assert set(ids) == {tagged.id, untagged.id}  # nothing filtered out
  assert ids[0] == tagged.id  # tagged doc lifted to the top


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


def test_desc_sort_breaks_timestamp_ties_by_score_desc():
  # Regression: sorting on (timestamp, -score) with reverse=True flipped the
  # secondary key too, so among same-timestamp hits the WEAKEST match was
  # returned first. Two items share a timestamp; the one matching the query
  # twice must outrank the one matching once.
  conn = _open_db()
  ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  items = [
    _make_item(content="tiebreak-token weak filler text", ts=ts, msg_id="weak"),
    _make_item(content="tiebreak-token tiebreak-token strong", ts=ts, msg_id="strong"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(sort="desc", include_consolidations=False)
  results = query(conn, "tiebreak-token", config=cfg)
  assert len(results) == 2
  assert results[0].timestamp == results[1].timestamp
  assert results[0].score >= results[1].score
