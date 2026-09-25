"""Jev hook in hybrid.query: each spec's final order after the downstream sort."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yaams import jev
from yaams.db import open_db
from yaams.ingest.base import Item, hash_id
from yaams.retrieve.hybrid import HybridQueryConfig, query
from yaams.schema import init_schema
from yaams.store import store_items

# FTS order is s0 > s1 > ... > s4 (more "alpha" ranks higher)
N = 5


def _conn(tmp_path):
  conn = open_db(tmp_path / "y.db")
  init_schema(conn, use_vec=False)
  items = [
    Item(id=hash_id("email", f"s{i}"), source="email", source_id=f"s{i}",
         timestamp=datetime(2025, 1, 1 + i, tzinfo=UTC), sender="a@x", recipients=["b@x"],
         content=" ".join(["alpha"] * (N - i)) + f" tag{i}", subject=f"s{i}")
    for i in range(N)
  ]
  store_items(conn, items, [[0.1]] * N, [[]] * N)
  return conn


def _stub(monkeypatch, nouls: dict[str, float]):
  calls = []

  def fake(state, items, criterion, **kw):
    calls.append((state, items, criterion, kw))
    return {i: nouls[t.split("]\n", 1)[0].rsplit("| ", 1)[1]] for i, t in items.items()
            if t.split("]\n", 1)[0].rsplit("| ", 1)[1] in nouls}

  monkeypatch.setattr(jev, "noul", fake)
  return calls


def _order(conn, spec):
  cfg = HybridQueryConfig(jev_spec=spec, include_consolidations=False, jev_question="q?",
                          jev_asked_on=datetime(2026, 4, 30, tzinfo=UTC))
  return [r.subject for r in query(conn, "alpha", embedding=None, config=cfg)]


def test_default_path_never_calls_jev(monkeypatch, tmp_path):
  calls = _stub(monkeypatch, {})
  cfg = HybridQueryConfig(include_consolidations=False)
  assert [r.subject for r in query(_conn(tmp_path), "alpha", config=cfg)] == [f"s{i}" for i in range(N)]
  assert calls == []


def test_state_and_text_match_rel1(monkeypatch, tmp_path):
  calls = _stub(monkeypatch, {"s0": 0.5})
  _order(_conn(tmp_path), "blend:1.0")
  state, items, criterion, kw = calls[0]
  assert state == jev.rel_state("q?", "2026-04-30")
  assert criterion == jev.REL_CRITERION and kw["criterion_version"] == "rel-1"
  assert next(iter(items.values())).startswith("[email | 2025-01-0")


def test_replace_orders_by_noul(monkeypatch, tmp_path):
  _stub(monkeypatch, {"s0": 0.1, "s1": 0.2, "s2": 0.9, "s3": 0.3, "s4": 0.8})
  assert _order(_conn(tmp_path), "replace") == ["s2", "s4", "s3", "s1", "s0"]


def test_blend_is_bounded_adjustment(monkeypatch, tmp_path):
  _stub(monkeypatch, {"s0": 0.0, "s1": 0.5, "s2": 0.5, "s3": 0.5, "s4": 1.0})
  order = _order(_conn(tmp_path), "blend:1.0")
  assert order.index("s4") < order.index("s0")


def test_gate_moves_at_most_one(monkeypatch, tmp_path):
  _stub(monkeypatch, {"s0": 0.1, "s1": 0.2, "s2": 0.3, "s3": 0.95, "s4": 0.85})
  order = _order(_conn(tmp_path), "gate:0.8")
  assert order == ["s3", "s0", "s1", "s2", "s4"]


def test_gate_does_not_fire_when_rank1_is_relevant(monkeypatch, tmp_path):
  _stub(monkeypatch, {"s0": 0.6, "s3": 0.99})
  assert _order(_conn(tmp_path), "gate:0.8") == [f"s{i}" for i in range(N)]


def test_filter_drops_low_noul_keeps_missing(monkeypatch, tmp_path):
  _stub(monkeypatch, {"s0": 0.9, "s1": 0.1, "s2": 0.2, "s3": 0.7})  # s4 missing
  conn = _conn(tmp_path)
  cfg = HybridQueryConfig(jev_spec="filter:0.5", include_consolidations=False)
  res = query(conn, "alpha", config=cfg)
  assert [r.subject for r in res] == ["s0", "s3", "s4"]
  assert res[-1].boosts.get("jev_missing") == 1.0 and "jev" not in res[-1].boosts


def test_unknown_spec_raises(monkeypatch, tmp_path):
  _stub(monkeypatch, {})
  with pytest.raises(ValueError):
    _order(_conn(tmp_path), "bogus:1")
