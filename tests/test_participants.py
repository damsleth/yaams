import sqlite3
from datetime import datetime

from yaams.enrich.participants import link_participants


def _db() -> sqlite3.Connection:
  db = sqlite3.connect(":memory:")
  db.executescript(
    "CREATE TABLE entities (id INTEGER PRIMARY KEY, canonical_name TEXT, entity_type TEXT, aliases TEXT,"
    " pending_review INTEGER NOT NULL DEFAULT 0);"
    "CREATE TABLE items (id TEXT, sender TEXT, recipients TEXT, ingested_at TEXT);"
    "CREATE TABLE item_entities (item_id TEXT, entity_id INTEGER, confidence REAL, source TEXT,"
    " PRIMARY KEY (item_id, entity_id));"
  )
  return db


def test_link_participants_incremental_and_keeps_content_links():
  db = _db()
  db.executescript(
    "INSERT INTO entities VALUES (1, 'Bob Smith', 'person', '[\"bob@x.no\", \"+47 900 00 000\"]', 1);"
    "INSERT INTO items VALUES ('a', 'Bob@x.no', '[\"me\"]', '2026-01-01T00:00:00+00:00');"
    "INSERT INTO items VALUES ('b', 'me', '[\"+4790000000\"]', '2026-01-01T00:00:00+00:00');"
    "INSERT INTO items VALUES ('c', 'me', '[\"Bob Smith\"]', '2025-01-01T00:00:00+00:00');"
    "INSERT INTO item_entities VALUES ('a', 1, 0.8, 'ner');"
  )
  stats = link_participants(db, ["me"], since=datetime.fromisoformat("2026-01-01T00:00:00+00:00"))
  assert stats["matched"] == 2 and stats["linked"] == 1, stats  # 'a' keeps its ner link, 'c' is too old
  assert db.execute("SELECT source FROM item_entities WHERE item_id='a'").fetchone()[0] == "ner"
  assert link_participants(db, ["me"])["linked"] == 1  # backfill picks up 'c'


def test_link_participants_alias_edge_cases():
  db = _db()
  db.executescript(
    "INSERT INTO entities VALUES (1, 'Alex Smith', 'person', '[\"Alex\", \"Al\"]', 1);"
    "INSERT INTO entities VALUES (2, 'Alex Jones', 'person', '[\"Alex\", \"Alex Smith\"]', 1);"
    "INSERT INTO entities VALUES (3, 'Nul Person', 'person', 'null', 1);"
    "INSERT INTO entities VALUES (4, 'Denied Guy', 'person', '[]', 2);"
    "INSERT INTO items VALUES ('shared', 'Alex', '[]', '2026-01-01T00:00:00+00:00');"
    "INSERT INTO items VALUES ('canon', 'Alex Smith', '{\"to\": \"x\"}', '2026-01-01T00:00:00+00:00');"
    "INSERT INTO items VALUES ('unique', 'al', '[\"Nul Person\", \"Denied Guy\"]', '2026-01-01T00:00:00+00:00');"
  )
  link_participants(db, ["me"])
  links = set(db.execute("SELECT item_id, entity_id FROM item_entities"))
  # a shared alias links nobody; a canonical name beats another person's alias;
  # JSON-null aliases and non-list recipients are tolerated; denied entities never link
  assert links == {("canon", 1), ("unique", 1), ("unique", 3)}
