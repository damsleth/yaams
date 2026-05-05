"""CLI-level regression tests using click's CliRunner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli


_MINIMAL_CONFIG = """
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'
  imessage:
    enabled: false
    chat_db_path: ~/none

embed:
  model: dummy
  dimension: 4

entities:
  dictionary: []
"""


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_MINIMAL_CONFIG.format(db_path=db))
  return cfg, db


def test_query_with_no_parse_does_not_log_parser_fallback(tmp_path):
  cfg_path, db_path = _write_config(tmp_path)

  runner = CliRunner()
  init = runner.invoke(cli, ["init-db", "--config", str(cfg_path)])
  assert init.exit_code == 0, init.output

  result = runner.invoke(
    cli,
    [
      "query",
      "--config", str(cfg_path),
      "--no-parse",
      "--no-vector",
      "anything",
    ],
  )
  assert result.exit_code == 0, result.output

  conn = sqlite3.connect(str(db_path))
  conn.row_factory = sqlite3.Row
  rows = conn.execute(
    "SELECT parser_fallback FROM queries ORDER BY ts DESC LIMIT 1"
  ).fetchall()
  conn.close()
  assert rows, "expected the query to be logged"
  assert rows[0]["parser_fallback"] == 0, (
    "--no-parse must not be logged as a parser fallback"
  )
