import sqlite3
from datetime import datetime

from yaams.enrich.participants import link_participants


def test_link_participants_incremental_and_keeps_content_links():
  db = sqlite3.connect(":memory:")
  db.executescript(
    "CREATE TABLE entities (id INTEGER PRIMARY KEY, canonical_name TEXT, entity_type TEXT, aliases TEXT);"
    "CREATE TABLE items (id TEXT, sender TEXT, recipients TEXT, ingested_at TEXT);"
    "CREATE TABLE item_entities (item_id TEXT, entity_id INTEGER, confidence REAL, source TEXT,"
    " PRIMARY KEY (item_id, entity_id));"
    "INSERT INTO entities VALUES (1, 'Bob Smith', 'person', '[\"bob@x.no\", \"+47 900 00 000\"]');"
    "INSERT INTO items VALUES ('a', 'Bob@x.no', '[\"me\"]', '2026-01-01T00:00:00+00:00');"
    "INSERT INTO items VALUES ('b', 'me', '[\"+4790000000\"]', '2026-01-01T00:00:00+00:00');"
    "INSERT INTO items VALUES ('c', 'me', '[\"Bob Smith\"]', '2025-01-01T00:00:00+00:00');"
    "INSERT INTO item_entities VALUES ('a', 1, 0.8, 'ner');"
  )
  stats = link_participants(db, ["me"], since=datetime.fromisoformat("2026-01-01T00:00:00+00:00"))
  assert stats["matched"] == 2 and stats["linked"] == 1, stats  # 'a' keeps its ner link, 'c' is too old
  assert db.execute("SELECT source FROM item_entities WHERE item_id='a'").fetchone()[0] == "ner"
  assert link_participants(db, ["me"])["linked"] == 1  # backfill picks up 'c'
