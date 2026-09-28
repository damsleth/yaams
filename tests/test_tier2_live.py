import json
import sqlite3
from datetime import UTC, datetime, timedelta

from yaams.cli.query import apply_tier2_live_config
from yaams.ingest.base import Item, hash_id
from yaams.retrieve import HybridQueryConfig, query
from yaams.schema import init_schema
from yaams.store import store_items

TS = datetime(2026, 9, 1, tzinfo=UTC)


def _note(rel: str) -> Item:
  return Item(
    id=hash_id("tier2_ledger", rel), source="tier2_ledger", source_id=rel, timestamp=TS,
    sender="me", recipients=[], content="vaktansvar rotation for the brkh group", subject=rel,
  )


def _setup(tmp_path, *, index_entries):
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  live, archived = _note("notes/05_open_loops/live.md"), _note("notes/05_open_loops/old.md")
  store_items(conn, [live, archived], [b"\x00" * 16] * 2, [[]] * 2)
  index = tmp_path / "08_indices" / "note_index.json"
  index.parent.mkdir()
  index.write_text(json.dumps({"entries": index_entries}))
  cfg = {"ingest": {"tier2_ledger": {"enabled": True, "notes_path": str(tmp_path)}}}
  return conn, cfg, live, archived


def test_archived_tier2_notes_are_hidden_from_every_search_path(tmp_path):
  entries = {"notes/05_open_loops/live.md": {"candidate": {"rel_path": "notes/05_open_loops/live.md"}}}
  conn, cfg, live, archived = _setup(tmp_path, index_entries=entries)
  qcfg = HybridQueryConfig(include_consolidations=False)
  apply_tier2_live_config(qcfg, cfg, conn)
  assert qcfg.exclude_item_ids == {archived.id}

  assert [r.id for r in query(conn, "vaktansvar", config=qcfg)] == [live.id]
  browse = HybridQueryConfig(
    include_consolidations=False, since=TS - timedelta(days=1), until=TS + timedelta(days=1),
    exclude_item_ids=qcfg.exclude_item_ids,
  )
  assert [r.id for r in query(conn, "no-such-token-xyz", config=browse)] == [live.id]


def test_unreadable_or_empty_index_hides_nothing(tmp_path):
  conn, cfg, _, _ = _setup(tmp_path, index_entries={})
  qcfg = HybridQueryConfig()
  apply_tier2_live_config(qcfg, cfg, conn)  # empty index: a broken build, not "all archived"
  assert qcfg.exclude_item_ids == frozenset()

  apply_tier2_live_config(qcfg, {"ingest": {"tier2_ledger": {"enabled": True, "notes_path": "/nope"}}}, conn)
  apply_tier2_live_config(qcfg, {}, conn)
  assert qcfg.exclude_item_ids == frozenset()
