from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from yaams.retrieve.parse import (
  ParsedQuery,
  _build_entity_resolver,
  _extract_json,
  parse_query,
)
from yaams.schema import init_schema
from yaams.synthesize.llm import LLMResponse


class _ScriptedAdapter:
  backend_name = "scripted"
  model_name = "test"

  def __init__(self, payloads: list[str]):
    self._payloads = list(payloads)
    self.calls = 0

  def complete(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0) -> LLMResponse:
    text = self._payloads[self.calls % len(self._payloads)]
    self.calls += 1
    return LLMResponse(text=text, backend=self.backend_name, model=self.model_name)


class _FailingAdapter:
  backend_name = "fail"
  model_name = None

  def complete(self, prompt, *, max_tokens=1024, temperature=0.0):
    raise RuntimeError("backend down")


def _seed_db_with_entity(canonical: str, aliases: list[str]) -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases) VALUES (?, ?, ?)",
    (canonical, "person", json.dumps(aliases)),
  )
  conn.commit()
  return conn


def test_extract_json_handles_fenced_output():
  raw = "```json\n{\"shape\": \"factual\"}\n```"
  assert _extract_json(raw) == {"shape": "factual"}


def test_extract_json_handles_prose_wrapped_object():
  raw = "Here you go:\n{\"shape\": \"factual\", \"entities\": []}\nthanks!"
  assert _extract_json(raw) == {"shape": "factual", "entities": []}


def test_extract_json_returns_none_for_non_json():
  assert _extract_json("totally not json") is None


def test_parse_query_happy_path_factual():
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "factual",
      "entities": [],
      "date_range": [None, None],
      "topic_terms": ["ATLAS"],
      "sort": "relevance",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("what is ATLAS", adapter)
  assert parsed.shape == "factual"
  assert parsed.fallback_used is False
  assert parsed.topic_terms == ["ATLAS"]


def test_parse_query_first_occurrence_with_iso_dates():
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "first_occurrence",
      "entities": [],
      "date_range": [None, None],
      "topic_terms": ["ATLAS"],
      "sort": "asc",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("when did I first hear about ATLAS", adapter)
  assert parsed.shape == "first_occurrence"
  assert parsed.sort == "asc"


def test_parse_query_resolves_alias_to_canonical():
  conn = _seed_db_with_entity("Bob Smith", ["Bob", "TN"])
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "factual",
      "entities": ["Bob"],
      "date_range": [None, None],
      "topic_terms": [],
      "sort": "relevance",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("hva sa Bob om ATLAS", adapter, conn)
  assert parsed.entities == ["Bob Smith"]
  assert parsed.fallback_used is False


def test_parse_query_drops_hallucinated_entity():
  conn = _seed_db_with_entity("Bob Smith", [])
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "factual",
      "entities": ["Hermione Granger"],
      "date_range": [None, None],
      "topic_terms": ["potions"],
      "sort": "relevance",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("did Hermione brew anything", adapter, conn)
  assert parsed.entities == []
  assert "Hermione Granger" in parsed.topic_terms


def test_parse_query_unknown_shape_falls_back():
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "garbled",
      "entities": [],
      "date_range": [None, None],
      "topic_terms": [],
      "sort": "relevance",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("anything", adapter)
  assert parsed.shape == "factual"
  assert parsed.fallback_used is True


def test_parse_query_malformed_json_falls_back():
  adapter = _ScriptedAdapter(["not json at all"])
  parsed = parse_query("hello", adapter)
  assert parsed.fallback_used is True
  assert parsed.shape == "factual"
  assert parsed.topic_terms == ["hello"]


def test_parse_query_handles_adapter_exception():
  parsed = parse_query("hello", _FailingAdapter())
  assert parsed.fallback_used is True
  assert parsed.shape == "factual"


def test_parse_query_empty_input_returns_fallback():
  parsed = parse_query("   ", _ScriptedAdapter(["{}"]))
  assert parsed.fallback_used is True
  assert parsed.topic_terms == []


def test_parse_query_norwegian_temporal_range():
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "temporal_range",
      "entities": [],
      "date_range": ["2026-04-01T00:00:00+00:00", "2026-04-30T23:59:59+00:00"],
      "topic_terms": ["SPEC", "proposal"],
      "sort": "relevance",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("Hva sa Alice om SPEC proposal i april?", adapter)
  assert parsed.shape == "temporal_range"
  start, end = parsed.date_range
  assert start == datetime(2026, 4, 1, tzinfo=UTC)
  assert end is not None and end.month == 4


def test_parse_query_synthesis_with_tier_preference():
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "synthesis",
      "entities": [],
      "date_range": [None, None],
      "topic_terms": ["YAAMS", "Tier 2"],
      "sort": "relevance",
      "prefer_tier": "tier2_ledger",
      "high_quality": False,
    })
  ])
  parsed = parse_query("what is my position on YAAMS Tier 2", adapter)
  assert parsed.shape == "synthesis"
  assert parsed.prefer_tier == "tier2_ledger"


def test_parse_query_bad_date_marks_fallback():
  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "temporal_range",
      "entities": [],
      "date_range": ["not-a-date", None],
      "topic_terms": [],
      "sort": "relevance",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("anytime", adapter)
  assert parsed.fallback_used is True
  assert parsed.date_range == (None, None)


def test_build_entity_resolver_handles_empty_db():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  resolver = _build_entity_resolver(conn, top_n=10)
  assert resolver.canonical_names == []
  assert resolver.resolve("anything") is None


def test_long_tail_entity_outside_prompt_top_n_still_resolves():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  for i in range(5):
    conn.execute(
      "INSERT INTO entities (canonical_name, entity_type, aliases) VALUES (?, ?, ?)",
      (f"Popular {i}", "person", json.dumps([])),
    )
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases) VALUES (?, ?, ?)",
    ("Long Tail Person", "person", json.dumps(["LT"])),
  )
  conn.commit()

  adapter = _ScriptedAdapter([
    json.dumps({
      "shape": "factual",
      "entities": ["LT"],
      "date_range": [None, None],
      "topic_terms": [],
      "sort": "relevance",
      "prefer_tier": None,
      "high_quality": False,
    })
  ])
  parsed = parse_query("hva sa LT", adapter, conn, top_entities=2)
  assert parsed.entities == ["Long Tail Person"]
  assert parsed.fallback_used is False


def test_parsed_query_to_json_round_trips():
  q = ParsedQuery(
    raw="hi",
    shape="factual",
    entities=["A"],
    date_range=(datetime(2026, 1, 1, tzinfo=UTC), None),
    topic_terms=["x"],
    sort="asc",
    prefer_tier="raw",
    high_quality=True,
    fallback_used=False,
  )
  data = json.loads(q.to_json())
  assert data["shape"] == "factual"
  assert data["entities"] == ["A"]
  assert data["date_range"][0].startswith("2026-01-01")
  assert data["sort"] == "asc"
  assert data["prefer_tier"] == "raw"
