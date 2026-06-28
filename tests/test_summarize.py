from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from yaams.synthesize.summarize import (
  build_prompt,
  fetch_new_items,
  summarize_ingest,
  summary_config,
)


def _items_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute(
    "CREATE TABLE items (id TEXT, source TEXT, timestamp TEXT, sender TEXT, "
    "subject TEXT, content TEXT, ingested_at TEXT)"
  )
  return conn


def test_summary_config_defaults_to_claude_on():
  sc = summary_config({})
  assert sc["enabled"] is True
  assert sc["backend"] == "claude"
  assert sc["to_inbox"] is True


def test_summary_config_safe_mode_on_by_default():
  assert summary_config({})["safe_mode"] is True
  assert summary_config({"summary": {"safe_mode": False}})["safe_mode"] is False


def test_claude_adapter_adds_safe_mode_flag():
  from yaams.synthesize.llm import ClaudeCliAdapter, llm_adapter_from_config

  plain = ClaudeCliAdapter()
  assert plain.safe_mode is False
  built = llm_adapter_from_config({"synth": {"backend": "claude", "safe_mode": True}})
  assert built.safe_mode is True


def test_summary_config_synth_sentinel_reuses_synth_backend():
  sc = summary_config({"synth": {"backend": "ollama", "model": "llama3.1"},
                       "summary": {"backend": "synth"}})
  assert sc["backend"] == "ollama"
  assert sc["model"] == "llama3.1"


def test_fetch_new_items_filters_by_run_start_and_truncates():
  conn = _items_db()
  run_start = datetime(2026, 6, 28, 10, 0, tzinfo=UTC)
  old = (run_start - timedelta(hours=1)).isoformat()
  new = (run_start + timedelta(seconds=5)).isoformat()
  conn.execute(
    "INSERT INTO items VALUES ('a','imessage','2026-06-20','x','','old msg',?)",
    (old,),
  )
  conn.execute(
    "INSERT INTO items VALUES ('b','teams','2026-06-28','y','subj',?,?)",
    ("z" * 500, new),
  )
  conn.commit()
  rows = fetch_new_items(conn, run_start, max_items=400, content_chars=300)
  assert len(rows) == 1
  assert rows[0]["source"] == "teams"
  assert rows[0]["content"].endswith("…")
  assert len(rows[0]["content"]) == 301  # 300 chars + ellipsis


def test_build_prompt_groups_by_source():
  items = [
    {"source": "teams", "date": "2026-06-28", "sender": "a", "subject": "", "content": "hi"},
    {"source": "imessage", "date": "2026-06-28", "sender": "b", "subject": "", "content": "yo"},
  ]
  prompt = build_prompt(items, total_new=2)
  assert "## source: imessage" in prompt
  assert "## source: teams" in prompt
  assert "Group by topic" in prompt


def test_summarize_ingest_skips_when_no_new_items():
  conn = _items_db()
  res = summarize_ingest(conn, {}, run_started_at=datetime.now(UTC), total_new=0)
  assert res.text is None
  assert res.note == "no new items"


def test_summarize_ingest_skips_dummy_backend():
  conn = _items_db()
  res = summarize_ingest(
    conn, {"summary": {"backend": "dummy"}},
    run_started_at=datetime.now(UTC), total_new=3,
  )
  assert res.text is None
  assert "dummy" in res.note


def test_summarize_ingest_handles_missing_cli_gracefully():
  # backend=subprocess with a command that doesn't exist -> note, no crash.
  conn = _items_db()
  run_start = datetime(2026, 6, 28, 10, 0, tzinfo=UTC)
  conn.execute(
    "INSERT INTO items VALUES ('b','teams','2026-06-28','y','',?,?)",
    ("hello", (run_start + timedelta(seconds=1)).isoformat()),
  )
  conn.commit()
  res = summarize_ingest(
    conn,
    {"summary": {"backend": "subprocess", "command": ["definitely-not-a-real-cli-xyz"],
                 "to_inbox": False}},
    run_started_at=run_start, total_new=1,
  )
  assert res.text is None
  assert "not found" in res.note
