"""Cross-encoder rerank stage (.plans/05-retrieval-rerank.md).

The cross-encoder is stubbed (a keyword scorer) so tests never download a model.
Both tests run the FTS-only path (embedding=None) — which also covers the
`--no-vector --rerank` combination.
"""
from __future__ import annotations

from datetime import UTC, datetime

from yaams.db import open_db
from yaams.ingest.base import Item, hash_id
from yaams.retrieve.hybrid import HybridQueryConfig, query
from yaams.schema import init_schema
from yaams.store import store_items


def _seed(conn) -> None:
  items = [
    Item(
      id=hash_id("email", "hi"), source="email", source_id="hi",
      timestamp=datetime(2025, 1, 1, tzinfo=UTC), sender="a@x", recipients=["b@x"],
      content="alpha alpha alpha alpha", subject="alpha",
    ),
    Item(
      id=hash_id("email", "lo"), source="email", source_id="lo",
      timestamp=datetime(2025, 1, 2, tzinfo=UTC), sender="a@x", recipients=["b@x"],
      content="alpha beta", subject="beta",
    ),
  ]
  store_items(conn, items, [[0.1], [0.1]], [[], []])


def test_rerank_reorders_pool(monkeypatch, tmp_path):
  # "alpha"-heavy item wins FTS/RRF; the cross-encoder stub favors "beta", so
  # the truly-relevant-but-lower-ranked item should be lifted to #1.
  conn = open_db(tmp_path / "y.db")
  init_schema(conn, use_vec=False)
  _seed(conn)

  import yaams.retrieve.rerank as rr
  monkeypatch.setattr(
    rr, "rerank_pairs",
    lambda q, pairs, model, **kw: [float(doc.count("beta")) for _, doc in pairs],
  )

  cfg = HybridQueryConfig(rerank_enabled=True, include_consolidations=False)
  res = query(conn, "alpha", embedding=None, config=cfg)
  assert res[0].subject == "beta"  # cross-encoder lifted the beta item to rank 1


def test_default_path_never_invokes_rerank(monkeypatch, tmp_path):
  conn = open_db(tmp_path / "y.db")
  init_schema(conn, use_vec=False)
  _seed(conn)

  calls = {"n": 0}

  def _tripwire(*a, **k):
    calls["n"] += 1
    return []

  import yaams.retrieve.rerank as rr
  monkeypatch.setattr(rr, "rerank_pairs", _tripwire)

  cfg = HybridQueryConfig(rerank_enabled=False, include_consolidations=False)
  res = query(conn, "alpha", embedding=None, config=cfg)
  assert calls["n"] == 0  # fast path must not touch the cross-encoder
  assert res  # and still returns the baseline results
