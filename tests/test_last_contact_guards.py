"""Guards from the review of the last-contact work: the occurrence lane only for
topic-free entity questions, promotion only for trustworthy names, participant
links surviving a retag, gold answers safe from every junk rule, and an
unattended junk pass that cannot run past its deadline."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from test_parse import _ScriptedAdapter, _seed_db_with_entity
from test_quality import T0, _item, _reason, _store
from test_quality import _open_db as _quality_db
from test_retrieve import _make_item, _open_db
from test_route import _parsed

from yaams import jev
from yaams.quality import MECH_SHORT, annotate_mechanical
from yaams.retrieve import HybridQueryConfig, query
from yaams.retrieve.parse import parse_query
from yaams.retrieve.route import route
from yaams.store import _replace_entity_links, store_items


def test_occurrence_lane_only_for_topic_free_entity_questions():
  base = HybridQueryConfig()
  speak = _parsed(shape="last_occurrence", entities=["Anne Hjort"], topic_terms=["speak", "Hjort"])
  assert route(speak, base).occurrence_browse
  budget = _parsed(shape="last_occurrence", entities=["Anne Hjort"], topic_terms=["budget"])
  assert not route(budget, base).occurrence_browse
  assert not route(_parsed(shape="factual", entities=["Anne Hjort"]), base).occurrence_browse
  assert not route(_parsed(shape="last_occurrence", topic_terms=["budget"]), base).occurrence_browse


def test_promotion_skips_uncurated_names_and_drops_the_matched_alias():
  conn = _seed_db_with_entity("Anne Kristine Hjort", ["Anne Hjort"])
  conn.execute("INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
               "VALUES ('Office 365', 'org', '[]', 1), ('Ola Nordmann', 'person', 'null', 1)")
  adapter = _ScriptedAdapter([json.dumps({
    "shape": "factual", "entities": [], "topic_terms": ["Anne Hjort", "office 365", "Ola Nordmann"],
  })])
  parsed = parse_query("anne hjort office 365 ola nordmann", adapter, conn)
  # NER-discovered org and an unlinked NER person stay topic terms; JSON-null aliases are fine
  assert parsed.entities == ["Anne Kristine Hjort"]
  assert parsed.topic_terms == ["office 365", "Ola Nordmann"]


def _bob(conn) -> int:
  conn.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('Bob Smith', 'person')")
  return conn.execute("SELECT id FROM entities WHERE canonical_name = 'Bob Smith'").fetchone()["id"]


def test_entity_retag_keeps_participant_links():
  conn = _open_db()
  item = _make_item(msg_id="x")
  store_items(conn, [item], [b"\x00" * 16], [[]])
  conn.execute("INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'participant')",
               (item.id, _bob(conn)))
  _replace_entity_links(conn, item.id, [])
  assert [r[0] for r in conn.execute("SELECT source FROM item_entities WHERE item_id = ?", (item.id,))] == [
    "participant"]


def test_occurrence_lane_applies_sender_filter_before_its_limit():
  conn = _open_db()
  eid = _bob(conn)
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  mine = _make_item(sender="me", content="ok", ts=base, msg_id="mine")
  theirs = _make_item(sender="Bob Smith", content="ok", ts=base + timedelta(days=1), msg_id="theirs")
  store_items(conn, [mine, theirs], [b"\x00" * 16] * 2, [[]] * 2)
  for it in (mine, theirs):
    conn.execute("INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'participant')",
                 (it.id, eid))
  cfg = HybridQueryConfig(top_k=1, sort="desc", include_consolidations=False, entity_filter=["Bob Smith"],
                          sender_filter=["me"], occurrence_browse=True)
  assert [r.id for r in query(conn, "zzz", config=cfg)] == [mine.id]


def test_mechanical_rules_never_hide_a_gold_answer():
  conn = _quality_db()
  gold, other = _item("imessage", 1, "ok", ts=T0), _item("imessage", 2, "ja", ts=T0.replace(minute=1))
  _store(conn, [gold, other])
  conn.execute("PRAGMA foreign_keys = OFF")
  conn.execute("INSERT INTO query_feedback (query_id, kind, result_id, ts) VALUES ('q', 'hit', ?, 'x')",
               (gold.id,))
  annotate_mechanical(conn)
  assert _reason(conn, gold) is None and _reason(conn, other) == MECH_SHORT


def test_deadline_mode_stops_after_a_failed_batch(monkeypatch, tmp_path):
  monkeypatch.setattr(jev, "JEV_DIR", tmp_path)
  monkeypatch.setattr(jev, "_cache", None)
  monkeypatch.setattr(jev, "BACKOFF", (0,))
  monkeypatch.setenv("TYPESAFE_API_KEY", "k")
  calls = []

  def urlopen(req, timeout=None):
    calls.append(timeout)
    raise jev.urllib.error.HTTPError(req.full_url, 529, "overloaded", {}, None)

  monkeypatch.setattr(jev.urllib.request, "urlopen", urlopen)
  out = jev.noul({}, {"a": "x", "b": "y"}, "c", criterion_version="t", tag="t", use_cache=False,
                 max_questions=1, workers=1, timeout=7, deadline_s=60)
  assert out == {} and calls == [7, 7]  # batch 1 tried twice, batch 2 never sent
