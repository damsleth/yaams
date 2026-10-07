"""Guards from the review of the last-contact work: the occurrence lane only for
topic-free entity questions, promotion only for trustworthy names, participant
links surviving a retag, gold answers safe from every junk rule, and an
unattended junk pass that cannot run past its deadline."""
from __future__ import annotations

import json
from dataclasses import replace
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


def test_occurrence_lane_prefers_participation_over_mentions():
  conn = _open_db()
  eid = _bob(conn)
  base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
  chat = _make_item(sender="Bob Smith", recipients=["me"], content="ok", ts=base, msg_id="chat")
  note = _make_item(source="notes", sender="me", content="ask Bob", ts=base + timedelta(days=5), msg_id="note")
  store_items(conn, [chat, note], [b"\x00" * 16] * 2, [[]] * 2)
  conn.execute("INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'participant')",
               (chat.id, eid))
  conn.execute("INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'dictionary')",
               (note.id, eid))
  cfg = HybridQueryConfig(top_k=5, sort="desc", include_consolidations=False, entity_filter=["Bob Smith"],
                          occurrence_browse=True, occurrence_contact=True)
  assert query(conn, "zzz", config=cfg)[0].id == chat.id
  older = _make_item(source="notes", sender="me", content="met Bob", ts=base - timedelta(days=9), msg_id="old")
  store_items(conn, [older], [b"\x00" * 16], [[]])
  conn.execute("INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, 'dictionary')",
               (older.id, eid))
  # "first spoke with" skips the older mention; "first heard about" counts it
  assert query(conn, "zzz", config=replace(cfg, sort="asc"))[0].id == chat.id
  assert query(conn, "zzz", config=replace(cfg, sort="asc", occurrence_contact=False))[0].id == older.id


def test_conversation_items_is_an_exchange_not_a_group_broadcast():
  from yaams.enrich.participants import conversation_items

  conn = _open_db()
  t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
  group = ["me", "Bob Smith", "x@y", "z@y"]
  items = [
    _make_item(thread_id="dm", sender="Bob Smith", recipients=["me"], ts=t, msg_id="dm"),
    _make_item(thread_id="roster", sender="boss@y", recipients=group, ts=t, msg_id="third"),
    _make_item(thread_id="code", sender="Bob Smith", recipients=group, ts=t, msg_id="broadcast"),
    _make_item(thread_id="plan", sender="Bob Smith", recipients=group, ts=t, msg_id="asked"),
    _make_item(thread_id="plan", sender="me", recipients=group, ts=t + timedelta(hours=1), msg_id="answered"),
  ]
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))
  kept = conversation_items(conn, [i.id for i in items], ["Bob Smith"], ["me"])
  # 1:1 counts; a third party's group message and Bob's unanswered broadcast do not;
  # a group thread where both wrote does
  assert kept == {items[0].id, items[3].id, items[4].id}


def test_promotion_drops_the_llms_near_miss_entity():
  from yaams.retrieve.parse import ParsedQuery, promote_entities

  p = ParsedQuery(raw="when did I first speak with Øystein Røvde?", entities=["Øistein"])
  promote_entities(p, {"øystein røvde": "Øystein Røvde"})
  assert p.entities == ["Øystein Røvde"]  # the LLM snapped Øystein to another person
  p = ParsedQuery(raw="did Anne talk to Bob Smith about nc", entities=["Norconsult", "Anne"])
  promote_entities(p, {"bob smith": "Bob Smith"})
  assert p.entities == ["Norconsult", "Anne", "Bob Smith"]  # unrelated entities stay


def test_first_occurrence_needs_the_words_once_text_matched(monkeypatch):
  from yaams.retrieve import hybrid

  conn = _open_db()
  t = datetime(2026, 1, 1, tzinfo=UTC)
  old = _make_item(content="dinner plans with the family", ts=t, msg_id="old")
  hit = _make_item(content="kickoff for the nocos project", ts=t + timedelta(days=90), msg_id="hit")
  store_items(conn, [old, hit], [b"\x00" * 16] * 2, [[]] * 2)
  # a vector neighbour that never says "nocos" (the 2025 chats in the real case)
  monkeypatch.setattr(hybrid, "_vec_search_items", lambda *a, **k: [("item", old.id, 0, 0.1)])
  cfg = HybridQueryConfig(top_k=5, sort="asc", include_consolidations=False)
  assert [r.id for r in query(conn, "nocos", embedding=[0.0] * 4, config=cfg)] == [hit.id]
  # with no text match at all, the vector pool is all there is and stays
  assert [r.id for r in query(conn, "zzzz", embedding=[0.0] * 4, config=cfg)] == [old.id]


def test_fallback_parse_keeps_first_and_last_questions():
  from yaams.retrieve.parse import _fallback

  assert _fallback("when did i last speak with Fredrik Nordmoen?").shape == "last_occurrence"
  assert _fallback("når snakket jeg med Anne for første gang?").shape == "first_occurrence"
  assert _fallback("when did I first hear about NOCOS").sort == "asc"
  # a date range or a list, not an occurrence
  assert _fallback("what did i do last week?").shape == "factual"
  assert _fallback("what are my last 10 messages with gustav").shape == "factual"
  # and the fallback's whole-question topic term still counts as topic-free
  parsed = _fallback("når snakket jeg med Anne Hjort for første gang?")
  parsed.entities = ["Anne Hjort"]
  assert route(parsed, HybridQueryConfig()).occurrence_browse


def test_contact_verbs_vs_hearsay():
  base = HybridQueryConfig()
  def contact(raw: str, shape: str = "first_occurrence") -> bool:
    return route(_parsed(raw=raw, shape=shape, entities=["Anne Hjort"]), base).occurrence_contact
  assert contact("when did I first speak with Anne Hjort?")
  assert contact("når snakket jeg sist med Anne Hjort?", "last_occurrence")
  assert not contact("when did I first hear about Anne Hjort?")
  assert not contact("når hørte jeg første gang om Anne Hjort?")


def test_received_only_consolidation_counts_as_participation():
  from yaams.retrieve.hybrid import _resolve_participant_allowlist

  conn = _open_db()
  conn.execute(
    "INSERT INTO consolidations (id, source, thread_id, start_timestamp, end_timestamp, participants,"
    " item_count, summary, raw_item_ids, consolidator_version, created_at)"
    " VALUES ('cons:x', 'teams', 't', '2026-01-01', '2026-01-01', '[\"bob\"]', 1, 's', ?, 'v', '2026-01-01')",
    (json.dumps(["read-only-item"]),),
  )
  conn.execute("INSERT INTO items (id, source, source_id, timestamp, sender, recipients, content, ingested_at)"
               " VALUES ('read-only-item', 'teams', 'x', '2026-01-01', 'bob', '[\"me\"]', 'hi', '2026-01-01')")
  assert "cons:x" in _resolve_participant_allowlist(conn, ["me"])[1]


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
