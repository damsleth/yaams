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
    _make_item(source="teams_swon", content="kim has a cat", msg_id="2"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))

  cfg = HybridQueryConfig(source_filter=["teams_swon"])
  results = query(conn, "kim", config=cfg)
  assert all(r.source == "teams_swon" for r in results)
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
