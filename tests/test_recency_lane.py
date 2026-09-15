"""Recency lane: a second candidate fetch over the trailing window, RRF-fused.

The property under test is the one decay could not deliver -- a recent match
for a common keyword reaches the pool at all -- plus the guards that keep the
lane out of queries that already scoped time or sort by timestamp."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from yaams.ingest.base import Item, hash_id
from yaams.retrieve import HybridQueryConfig, query
from yaams.retrieve.hybrid import _corpus_now
from yaams.schema import init_schema
from yaams.store import store_items

NEWEST = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _open_db():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _item(msg_id: str, content: str, ts: datetime) -> Item:
  return Item(
    id=hash_id("imessage", f"t:{msg_id}"),
    source="imessage",
    source_id=f"t:{msg_id}",
    timestamp=ts,
    sender="alice@example.test",
    recipients=[],
    content=content,
    subject="",
    thread_id="t",
  )


def _seed(conn, n_old: int = 60):
  """Many old docs that match the keyword strongly, one recent doc that
  matches weakly. bm25 alone buries the recent one below per_index_k."""
  items = [
    _item(f"old{i}", "vakt vakt vakt vakt vakt vaktliste vaktplan", NEWEST - timedelta(days=400 + i))
    for i in range(n_old)
  ]
  items.append(_item("recent", "husk vakt på lørdag, og ta med kaffe til alle", NEWEST))
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))
  return hash_id("imessage", "t:recent")


def test_corpus_now_is_the_newest_item_not_wall_clock():
  conn = _open_db()
  _seed(conn, n_old=2)
  assert _corpus_now(conn) == NEWEST


def test_corpus_now_falls_back_to_wall_clock_on_an_empty_store():
  conn = _open_db()
  assert abs((_corpus_now(conn) - datetime.now(UTC)).total_seconds()) < 5


def test_lane_admits_a_recent_match_the_general_fetch_buries():
  conn = _open_db()
  recent = _seed(conn)
  base = HybridQueryConfig(top_k=10, per_index_k=20)

  without = [r.id for r in query(conn, "vakt", config=base)]
  assert recent not in without, "precondition: bm25 alone buries the recent doc"

  with_lane = [r.id for r in query(conn, "vakt", config=HybridQueryConfig(
    top_k=10, per_index_k=20, recency_lane_days=60,
  ))]
  assert recent in with_lane


def test_lane_never_removes_an_old_result():
  conn = _open_db()
  _seed(conn)
  base = HybridQueryConfig(top_k=50, per_index_k=20)
  without = {r.id for r in query(conn, "vakt", config=base)}
  with_lane = {r.id for r in query(conn, "vakt", config=HybridQueryConfig(
    top_k=50, per_index_k=20, recency_lane_days=60,
  ))}
  assert without <= with_lane, "the lane only adds candidates"


def test_lane_is_skipped_when_the_caller_already_scoped_time():
  conn = _open_db()
  recent = _seed(conn)
  # a window that excludes the recent doc must stay excluded: the lane would
  # otherwise silently widen a user's explicit --since
  cfg = HybridQueryConfig(
    top_k=10, per_index_k=20, recency_lane_days=60,
    since=NEWEST - timedelta(days=500), until=NEWEST - timedelta(days=300),
  )
  assert recent not in [r.id for r in query(conn, "vakt", config=cfg)]


def test_lane_is_skipped_for_timestamp_sorts():
  conn = _open_db()
  recent = _seed(conn)
  # timestamp sorts already put the newest first via their own path; the lane
  # must not perturb the candidate set they sort. With sort=desc the recent doc
  # surfaces regardless, so assert the lane is a no-op on the result set.
  a = [r.id for r in query(conn, "vakt", config=HybridQueryConfig(top_k=10, sort="desc"))]
  b = [r.id for r in query(conn, "vakt", config=HybridQueryConfig(
    top_k=10, sort="desc", recency_lane_days=60,
  ))]
  assert a == b
  assert recent in a


def test_config_knob_is_opt_in_and_parsed():
  from yaams.cli.query import apply_recency_lane_config

  qcfg = HybridQueryConfig()
  apply_recency_lane_config(qcfg, {"retrieve": {}})
  assert qcfg.recency_lane_days == 0.0

  apply_recency_lane_config(qcfg, {"retrieve": {"recency_lane": {"days": 60}}})
  assert qcfg.recency_lane_days == 60.0


def test_recency_now_pins_the_lane_window_to_the_callers_clock():
  conn = _open_db()
  recent = _seed(conn)
  # Replay a query "asked" a year before the newest item: nothing in the store
  # is recent relative to that clock, so the lane must not admit the new doc.
  cfg = HybridQueryConfig(
    top_k=10, per_index_k=20, recency_lane_days=60,
    recency_now=NEWEST - timedelta(days=365),
  )
  assert recent not in [r.id for r in query(conn, "vakt", config=cfg)]
  # and pinned to the corpus edge it behaves exactly like the default
  cfg2 = HybridQueryConfig(top_k=10, per_index_k=20, recency_lane_days=60, recency_now=NEWEST)
  assert recent in [r.id for r in query(conn, "vakt", config=cfg2)]


def test_decay_measures_age_from_recency_now_when_pinned():
  from yaams.retrieve.hybrid import _apply_recency_decay

  ts = NEWEST
  # pinned to the item's own time: age 0, factor 1.0, score untouched
  pinned = HybridQueryConfig(recency_decay_tau_days=60, recency_decay_floor=0.2, recency_now=ts)
  assert _apply_recency_decay(1.0, "imessage", ts, pinned) == 1.0
  # pinned a year later: floored
  later = HybridQueryConfig(
    recency_decay_tau_days=60, recency_decay_floor=0.2, recency_now=ts + timedelta(days=365)
  )
  assert _apply_recency_decay(1.0, "imessage", ts, later) == 0.2
