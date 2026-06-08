"""``yaams review`` non-interactive paths must work against a read-only DB.

Regression coverage: --stats/--queue/--json previously opened the DB
read-write and ran init_schema, which failed with "attempt to write a
readonly database" against the live store. Only the TUI writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli
from yaams.signals import log_query

_MIN = """
db_path: {db_path}
ingest:
  since: '2025-01-01T00:00:00Z'
embed:
  model: dummy
  dimension: 4
entities:
  dictionary: []
"""


def _setup(tmp_path: Path) -> tuple[Path, Path]:
  cfg = tmp_path / "config.yaml"
  db_path = tmp_path / "data.db"
  cfg.write_text(_MIN.format(db_path=db_path))
  result = CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  assert result.exit_code == 0, result.output
  return cfg, db_path


def _log_one(db_path: Path, qid: str = "q_ro") -> None:
  conn = sqlite3.connect(db_path)
  conn.row_factory = sqlite3.Row
  try:
    log_query(
      conn,
      query_id=qid,
      text="what happened",
      top_k=10,
      source_filter=None,
      since=None,
      until=None,
      results=[],
    )
  finally:
    conn.close()


def test_review_stats_json_on_readonly_db(tmp_path):
  cfg, db_path = _setup(tmp_path)
  _log_one(db_path)
  os.chmod(db_path, 0o444)  # any write attempt now errors

  result = CliRunner().invoke(cli, ["review", "--stats", "--json", "--config", str(cfg)])
  assert result.exit_code == 0, result.output
  data = json.loads(result.output)
  assert data["total_queries"] == 1


def test_review_queue_json_on_readonly_db(tmp_path):
  cfg, db_path = _setup(tmp_path)
  _log_one(db_path, qid="q_queue")
  os.chmod(db_path, 0o444)

  result = CliRunner().invoke(cli, ["review", "--queue", "--json", "--config", str(cfg)])
  assert result.exit_code == 0, result.output
  payload = json.loads(result.output)
  assert [q["query_id"] for q in payload["queries"]] == ["q_queue"]


def test_review_queue_text_on_readonly_db(tmp_path):
  cfg, db_path = _setup(tmp_path)
  _log_one(db_path, qid="q_text")
  os.chmod(db_path, 0o444)

  result = CliRunner().invoke(cli, ["review", "--queue", "--config", str(cfg)])
  assert result.exit_code == 0, result.output
  assert "q_text" in result.output


def test_review_stats_json_uninitialized_db_emits_structured_error(tmp_path):
  cfg = tmp_path / "config.yaml"
  db_path = tmp_path / "data.db"
  cfg.write_text(_MIN.format(db_path=db_path))
  db_path.touch()  # exists but no schema; read-only open can't create it

  result = CliRunner().invoke(cli, ["review", "--stats", "--json", "--config", str(cfg)])
  assert result.exit_code == 1
  envelope = json.loads(result.output)
  assert envelope["ok"] is False
  assert envelope["error"]["code"] == "db_query_failed"
  assert "init-db" in envelope["error"]["hint"]


def test_review_stats_json_missing_db_emits_structured_error(tmp_path):
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_MIN.format(db_path=tmp_path / "nope.db"))

  result = CliRunner().invoke(cli, ["review", "--stats", "--json", "--config", str(cfg)])
  assert result.exit_code == 1
  envelope = json.loads(result.output)
  assert envelope["ok"] is False
  assert envelope["error"]["code"] == "db_open_failed"
