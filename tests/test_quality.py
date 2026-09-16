"""Mechanical junk annotation: the three rules, the two guards that keep them
honest (duplicates keyed by thread+sender+day, calendars untouched), and the
retrieval-side exclusion that reads the annotation."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from yaams.ingest.base import Item, hash_id
from yaams.quality import (
  MECH_DUP,
  MECH_REACTION,
  MECH_SHORT,
  annotate_mechanical,
  effective_corpus,
)
from yaams.retrieve import HybridQueryConfig, query
from yaams.schema import init_schema
from yaams.store import store_items

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _open_db():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _item(source, msg_id, content, ts=T0, thread="t1", sender="alice"):
  return Item(
    id=hash_id(source, f"{thread}:{msg_id}"), source=source, source_id=f"{thread}:{msg_id}",
    timestamp=ts, sender=sender, recipients=[], content=content, subject="", thread_id=thread,
  )


def _store(conn, items):
  store_items(conn, items, [b"\x00" * 16] * len(items), [[]] * len(items))


def _reason(conn, item):
  return conn.execute("SELECT junk_reason FROM items WHERE id=?", (item.id,)).fetchone()[0]


def test_migration_added_the_column_and_indexes():
  conn = _open_db()
  cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
  assert "junk_reason" in cols
  idx = {r[1] for r in conn.execute("PRAGMA index_list(items)")}
  assert {"idx_items_junk_reason", "idx_items_thread_ts"} <= idx


def test_short_and_reaction_rules_on_messaging_sources():
  conn = _open_db()
  short = _item("imessage", "1", "ok")
  react = _item("imessage", "2", "Liked “see you at 5”")
  real = _item("imessage", "3", "husk vakt på lørdag, ta med kaffe")
  _store(conn, [short, react, real])
  stats = annotate_mechanical(conn)
  assert (stats[MECH_SHORT], stats[MECH_REACTION]) == (1, 1)
  assert _reason(conn, short) == MECH_SHORT
  assert _reason(conn, react) == MECH_REACTION
  assert _reason(conn, real) is None


def test_duplicates_are_keyed_by_thread_sender_and_day_and_keep_the_earliest():
  conn = _open_db()
  a = _item("imessage", "1", "sender du referatet?", ts=T0)
  b = _item("imessage", "2", "sender du referatet?", ts=T0 + timedelta(minutes=5))       # true dup
  other_thread = _item("imessage", "3", "sender du referatet?", thread="t2")             # not a dup
  other_day = _item("imessage", "4", "sender du referatet?", ts=T0 + timedelta(days=1))  # not a dup
  other_sender = _item("imessage", "5", "sender du referatet?", sender="bob")            # not a dup
  _store(conn, [a, b, other_thread, other_day, other_sender])
  stats = annotate_mechanical(conn)
  assert stats[MECH_DUP] == 1
  assert _reason(conn, a) is None, "the earliest row is the one kept"
  assert _reason(conn, b) == MECH_DUP
  assert all(_reason(conn, x) is None for x in (other_thread, other_day, other_sender))


def test_calendars_and_long_form_sources_are_never_touched():
  conn = _open_db()
  cal1 = _item("calendar_swon", "1", "Lunch", ts=T0)
  cal2 = _item("calendar_swon", "2", "Lunch", ts=T0 + timedelta(days=7))
  note = _item("notes", "1", "ok")
  chat = _item("chats", "1", "ok")
  _store(conn, [cal1, cal2, note, chat])
  stats = annotate_mechanical(conn)
  assert stats[MECH_SHORT] == stats[MECH_DUP] == stats[MECH_REACTION] == 0
  assert all(_reason(conn, x) is None for x in (cal1, cal2, note, chat))


def test_annotation_is_idempotent_and_dry_run_writes_nothing():
  conn = _open_db()
  _store(conn, [_item("imessage", "1", "ok"), _item("imessage", "2", "ja")])
  dry = annotate_mechanical(conn, dry_run=True)
  assert dry[MECH_SHORT] == 2
  assert conn.execute("SELECT COUNT(*) FROM items WHERE junk_reason IS NOT NULL").fetchone()[0] == 0
  first = annotate_mechanical(conn)
  second = annotate_mechanical(conn)
  assert first[MECH_SHORT] == 2 and second[MECH_SHORT] == 0


def test_effective_corpus_separates_physical_from_retrievable():
  conn = _open_db()
  _store(conn, [_item("imessage", "1", "ok"), _item("imessage", "2", "a real message about vakt")])
  annotate_mechanical(conn)
  ec = effective_corpus(conn)
  assert ec["items_total"] == 2 and ec["items_retrievable"] == 1
  assert ec["annotated"] == {MECH_SHORT: 1}


def test_retrieval_excludes_annotated_items_only_when_asked():
  conn = _open_db()
  junk = _item("imessage", "1", "vakt")                    # 4 chars -> mech:short
  real = _item("imessage", "2", "husk vakt på lørdag, ta med kaffe")
  _store(conn, [junk, real])
  annotate_mechanical(conn)
  default = {r.id for r in query(conn, "vakt", config=HybridQueryConfig(top_k=10))}
  assert {junk.id, real.id} <= default, "annotation alone changes nothing"
  excluded = {r.id for r in query(conn, "vakt", config=HybridQueryConfig(top_k=10, exclude_junk=True))}
  assert real.id in excluded and junk.id not in excluded
