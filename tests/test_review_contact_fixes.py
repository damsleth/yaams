"""Regressions from the heavy review of the last-contact work (2026-10-12): each
test is one reported failure scenario."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from test_parse import _seed_db_with_entity
from test_retrieve import _make_item, _open_db
from test_route import _parsed

from yaams.enrich.participants import conversation_items
from yaams.retrieve import HybridQueryConfig, query
from yaams.retrieve.parse import ParsedQuery, _alias_supported, promote_entities
from yaams.retrieve.route import route
from yaams.store import store_items

T = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _bob(conn) -> int:
  conn.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('Bob Smith', 'person')")
  return conn.execute("SELECT id FROM entities WHERE canonical_name = 'Bob Smith'").fetchone()["id"]


def _link(conn, items, eid, source="participant"):
  for it in items:
    conn.execute("INSERT OR REPLACE INTO item_entities (item_id, entity_id, source) VALUES (?, ?, ?)",
                 (it.id, eid, source))


def test_as_of_reply_after_the_cutoff_does_not_count():
  conn = _open_db()
  post = _make_item(thread_id="g", sender="Bob Smith", recipients=["me", "x", "y"], ts=T, msg_id="post")
  reply = _make_item(thread_id="g", sender="me", recipients=["Bob Smith", "x", "y"], ts=T + timedelta(hours=1),
                     msg_id="reply")
  store_items(conn, [post, reply], [b"\x00" * 16] * 2, [[]] * 2)
  ids = [post.id, reply.id]
  assert post.id in conversation_items(conn, ids, ["Bob Smith"], ["me"])
  assert post.id not in conversation_items(conn, ids, ["Bob Smith"], ["me"], until=T + timedelta(minutes=30))


def test_lane_marks_an_item_already_in_the_pool():
  conn = _open_db()
  eid = _bob(conn)
  newest = _make_item(sender="Bob Smith", recipients=["me"], content="bob says nocos", ts=T, msg_id="n")
  store_items(conn, [newest], [b"\x00" * 16], [[]])
  _link(conn, [newest], eid)
  cfg = HybridQueryConfig(top_k=5, sort="desc", include_consolidations=False, entity_filter=["Bob Smith"],
                          occurrence_browse=True, query_shape="last_occurrence")
  res = query(conn, "nocos", config=cfg)
  assert res and res[0].id == newest.id and "occurrence_browse" in res[0].boosts


def test_lane_keeps_retired_items_out():
  conn = _open_db()
  eid = _bob(conn)
  old = _make_item(sender="Bob Smith", recipients=["me"], ts=T, msg_id="old")
  retired = _make_item(sender="Bob Smith", recipients=["me"], ts=T + timedelta(days=3), msg_id="retired")
  store_items(conn, [old, retired], [b"\x00" * 16] * 2, [[]] * 2)
  _link(conn, [old, retired], eid)
  cfg = HybridQueryConfig(top_k=5, sort="desc", include_consolidations=False, entity_filter=["Bob Smith"],
                          occurrence_browse=True, exclude_item_ids=frozenset({retired.id}))
  assert retired.id not in [r.id for r in query(conn, "zzz", config=cfg)]


def test_contact_keeps_a_short_junk_reply():
  conn = _open_db()
  eid = _bob(conn)
  talk = _make_item(sender="Bob Smith", recipients=["me"], content="long real conversation", ts=T, msg_id="t")
  ok = _make_item(sender="Bob Smith", recipients=["me"], content="ok", ts=T + timedelta(days=1), msg_id="ok")
  store_items(conn, [talk, ok], [b"\x00" * 16] * 2, [[]] * 2)
  conn.execute("UPDATE items SET junk_reason = 'mech:short' WHERE id = ?", (ok.id,))
  _link(conn, [talk, ok], eid)
  cfg = HybridQueryConfig(top_k=5, sort="desc", include_consolidations=False, entity_filter=["Bob Smith"],
                          occurrence_browse=True, occurrence_contact=True, exclude_junk=True,
                          participant_filter=["me"])
  assert query(conn, "zzz", config=cfg)[0].id == ok.id


def test_last_sort_orders_a_session_by_its_end():
  conn = _open_db()
  eid = _bob(conn)
  standalone = _make_item(sender="Bob Smith", recipients=["me"], ts=T + timedelta(hours=1, minutes=30),
                          msg_id="solo")
  store_items(conn, [standalone], [b"\x00" * 16], [[]])
  _link(conn, [standalone], eid)
  member = _make_item(thread_id="s", sender="Bob Smith", recipients=["me"], ts=T + timedelta(hours=2), msg_id="m")
  store_items(conn, [member], [b"\x00" * 16], [[]])
  _link(conn, [member], eid)
  conn.execute(
    "INSERT INTO consolidations (id, source, thread_id, start_timestamp, end_timestamp, participants, item_count,"
    " summary, raw_item_ids, consolidator_version, created_at) VALUES ('cons:s', 'imessage', 's', ?, ?, ?, 1, 's',"
    " ?, 'v', ?)",
    (T.isoformat(), (T + timedelta(hours=2)).isoformat(), json.dumps(["Bob Smith"]), json.dumps([member.id]),
     T.isoformat()),
  )
  conn.execute("UPDATE items SET consolidated_into = 'cons:s' WHERE id = ?", (member.id,))
  cfg = HybridQueryConfig(top_k=5, sort="desc", entity_filter=["Bob Smith"], occurrence_browse=True)
  # the session started 08:00 but ended 10:00, after the 09:30 standalone message
  assert query(conn, "zzz", config=cfg)[0].id == "cons:s"


def test_explicit_sort_keeps_owner_identities():
  parsed = _parsed(shape="last_occurrence", entities=["Bob Smith"])
  cfg = route(parsed, HybridQueryConfig(), explicit_sort=True, self_identities=["me", "me@x.no"])
  assert cfg.participant_filter == ["me", "me@x.no"]


def test_promotion_cuts_the_name_out_of_a_whole_question_topic():
  p = ParsedQuery(raw="when did i last speak with bob smith?", entities=[],
                  topic_terms=["when did i last speak with bob smith?"])
  promote_entities(p, {"bob smith": "Robert Jones"})
  assert p.entities == ["Robert Jones"]
  assert p.topic_terms == ["when did i last speak with ?"]


def test_alias_supported_entity_survives_near_miss_removal():
  conn = _seed_db_with_entity("Martina", ["Tina"])
  p = ParsedQuery(raw="Summarize Tina and Martin Hansen", entities=["Martina"])
  promote_entities(p, {"martin hansen": "Martin Hansen"}, _alias_supported(conn, p))
  assert p.entities == ["Martina", "Martin Hansen"]
