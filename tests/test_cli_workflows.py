from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli
from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.ingest.base import Item, hash_id
from yaams.schema import init_schema
from yaams.store import resolve_entity_id, store_items

_CONFIG_NO_SOURCES = """
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'
  imessage:
    enabled: false
    chat_db_path: ~/none
  signal:
    enabled: false
  email:
    enabled: false
  notes:
    enabled: false
  tier2_ledger:
    enabled: false
  github:
    enabled: false
    username: ''

embed:
  model: dummy
  dimension: 4

entities:
  dictionary: []
"""


def _config(tmp_path: Path) -> Path:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG_NO_SOURCES.format(db_path=db))
  return cfg


def _seed_entities_for_maintenance(cfg_path: Path) -> str:
  conn = open_db(get_db_path(load_config(str(cfg_path))))
  init_schema(conn, embedding_dim=4, use_vec=False)
  item = Item(
    id=hash_id("email", "<hamas@example.test>"),
    source="email",
    source_id="<hamas@example.test>",
    timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    recipients=["bob@example.test"],
    content="Hamas and Hamas' are the same edge-punctuation entity.",
  )
  store_items(conn, [item], [[0.1, 0.2, 0.3, 0.4]], [[]])
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
    "VALUES ('Hamas', 'org', '[]', 1)"
  )
  clean_id = resolve_entity_id(conn, "Hamas")
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
    "VALUES (?, 'org', '[]', 1)",
    ("Hamas'",),
  )
  dirty_id = resolve_entity_id(conn, "Hamas'")
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
    "VALUES ('Old Junk', 'org', '[]', 1)"
  )
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, confidence, source) "
    "VALUES (?, ?, 0.7, 'ner')",
    (item.id, clean_id),
  )
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, confidence, source) "
    "VALUES (?, ?, 0.7, 'ner')",
    (item.id, dirty_id),
  )
  conn.commit()
  conn.close()
  return item.id


def test_refresh_skip_ingest_runs_safe_maintenance(tmp_path: Path):
  cfg = _config(tmp_path)
  item_id = _seed_entities_for_maintenance(cfg)

  result = CliRunner().invoke(
    cli,
    ["refresh", "--config", str(cfg), "--skip-ingest", "--skip-assoc"],
  )

  assert result.exit_code == 0, result.output
  assert "Safe maintenance complete" in result.output
  conn = open_db(get_db_path(load_config(str(cfg))), readonly=True)
  try:
    clean_id = resolve_entity_id(conn, "Hamas")
    assert clean_id is not None
    assert resolve_entity_id(conn, "Hamas'") is None
    assert resolve_entity_id(conn, "Old Junk") is None
    rows = conn.execute(
      "SELECT entity_id FROM item_entities WHERE item_id = ?", (item_id,)
    ).fetchall()
    assert [row["entity_id"] for row in rows] == [clean_id]
  finally:
    conn.close()


def test_refresh_json_envelope(tmp_path: Path):
  cfg = _config(tmp_path)

  result = CliRunner().invoke(cli, ["refresh", "--config", str(cfg), "--json"])

  assert result.exit_code == 0, result.output
  payload = json.loads(result.output.strip())
  assert payload["command"] == "refresh"
  assert payload["ok"] is True
  assert payload["stats"]["ingest_ran"] is True
  assert payload["stats"]["ingest"]["command"] == "ingest"
  assert "maintenance" in payload["stats"]


def test_curate_non_tty_runs_safe_steps_and_skips_interactive(tmp_path: Path):
  cfg = _config(tmp_path)
  _seed_entities_for_maintenance(cfg)

  result = CliRunner().invoke(cli, ["curate", "--config", str(cfg), "--skip-assoc"])

  assert result.exit_code == 0, result.output
  assert "Safe maintenance complete" in result.output
  assert "Merge suggestions" in result.output
  assert "Prune suggestions" in result.output
  assert "Interactive dedupe skipped" in result.output
  assert "Interactive discover skipped" in result.output


def test_curate_rejects_json(tmp_path: Path):
  cfg = _config(tmp_path)

  result = CliRunner().invoke(cli, ["curate", "--config", str(cfg), "--json"])

  assert result.exit_code == 1
  assert "interactive command" in result.output.lower()
