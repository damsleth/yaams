"""CLI-level regression tests using click's CliRunner."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli
from yaams.db import open_db
from yaams.ingest.base import Item, hash_id
from yaams.schema import init_schema
from yaams.store import resolve_entity_id, store_items

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


def test_entities_discover_edit_merges_original_candidate(tmp_path):
  cfg_path, db_path = _write_config(tmp_path)
  conn = open_db(db_path)
  init_schema(conn, embedding_dim=4, use_vec=False)
  item = Item(
    id=hash_id("email", "<acme-colon@example.test>"),
    source="email",
    source_id="<acme-colon@example.test>",
    timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    recipients=["bob@example.test"],
    content="Acme: showed up with punctuation in the NER span.",
  )
  store_items(conn, [item], [[0.1, 0.2, 0.3, 0.4]], [[("Acme:", "org", 0.7, "ner")]])
  original_id = resolve_entity_id(conn, "Acme:")
  conn.close()
  assert original_id is not None

  result = CliRunner().invoke(
    cli,
    ["entities", "discover", "--config", str(cfg_path), "--min-count", "1"],
    input="e\nAcme\norg\nAC, Acme Inc\n",
  )

  assert result.exit_code == 0, result.output
  assert "Added 'Acme'." in result.output

  conn = open_db(db_path)
  target_id = resolve_entity_id(conn, "Acme")
  assert target_id is not None
  assert resolve_entity_id(conn, "Acme:") is None
  row = conn.execute(
    "SELECT pending_review, aliases FROM entities WHERE id = ?", (target_id,)
  ).fetchone()
  assert row["pending_review"] == 0
  assert json.loads(row["aliases"]) == ["AC", "Acme Inc", "Acme:"]
  link = conn.execute(
    "SELECT entity_id, source FROM item_entities WHERE item_id = ?", (item.id,)
  ).fetchone()
  assert dict(link) == {"entity_id": target_id, "source": "dictionary"}

  second = CliRunner().invoke(
    cli,
    ["entities", "discover", "--config", str(cfg_path), "--min-count", "1"],
  )
  conn.close()
  assert second.exit_code == 0, second.output
  assert "No NER candidates" in second.output
