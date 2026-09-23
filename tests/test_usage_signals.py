import sqlite3

from yaams.schema import init_schema
from yaams.signals.usage import item_usage, stale_tier2


def _q(conn, qid, provenance, results, ts="2026-09-01T00:00:00+00:00"):
  conn.execute(
    "INSERT INTO queries (id, text, top_k, ts, provenance) VALUES (?, 'q', 5, ?, ?)",
    (qid, ts, provenance),
  )
  for rank, (rid, cited) in enumerate(results, 1):
    conn.execute(
      "INSERT INTO query_results (query_id, rank, result_id, kind, cited) VALUES (?, ?, ?, 'item', ?)",
      (qid, rank, rid, cited),
    )


def test_item_usage_counts_real_traffic_only():
  conn = sqlite3.connect(":memory:")
  init_schema(conn, embedding_dim=4)
  _q(conn, "q1", "mcp", [("a", 1), ("b", 0)])
  _q(conn, "q2", "cli", [("a", 0)], ts="2026-09-10T00:00:00+00:00")
  _q(conn, "q3", "test", [("a", 1)])
  _q(conn, "q4", "cli", [("b", 1)])
  conn.execute("INSERT INTO query_feedback (query_id, kind, ts) VALUES ('q4', 'noise', 'x')")

  usage = item_usage(conn)
  assert (usage["a"].surfaced_count, usage["a"].cited_count) == (2, 1)
  assert usage["a"].last_surfaced_at.startswith("2026-09-10")
  assert (usage["b"].surfaced_count, usage["b"].cited_count) == (1, 0)

  from datetime import UTC, datetime

  from yaams.ingest.base import Item
  from yaams.store import store_items

  ts = datetime(2026, 1, 1, tzinfo=UTC)
  items = [
    Item(id=i, source="tier2_ledger", source_id=f"n/{i}.md", timestamp=ts, sender="me",
         recipients=[], content="x" * 30, subject=None, thread_id=None, raw_metadata={})
    for i in ("a", "z")
  ]
  store_items(conn, items, [b"\x00" * 16] * 2, [[]] * 2)
  assert stale_tier2(conn, usage, since_ts="2026-09-05") == [("z", "n/z.md")]
  assert stale_tier2(conn, usage, since_ts="2026-09-20") == [("a", "n/a.md"), ("z", "n/z.md")]


def test_later_bad_result_does_not_mask_noise_verdict():
  conn = sqlite3.connect(":memory:")
  init_schema(conn, embedding_dim=4)
  _q(conn, "q1", "cli", [("a", 0)])
  conn.execute("INSERT INTO query_feedback (query_id, kind, ts) VALUES ('q1', 'noise', 'x')")
  conn.execute("INSERT INTO query_feedback (query_id, kind, result_id, ts) VALUES ('q1', 'bad_result', 'a', 'y')")
  assert item_usage(conn) == {}
