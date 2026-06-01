from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from yaams.cli import cli
from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.store import resolve_entity_id

_CONFIG = """
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'

embed:
  model: dummy
  dimension: 4

entities:
  dictionary:
    - canonical: Crayon
      type: org

synthesize:
  llm:
    backend: dummy
"""


def _config(tmp_path: Path) -> Path:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG.format(db_path=db))
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  return cfg


def _run(cfg, *args):
  return CliRunner().invoke(cli, [*args, "--config", str(cfg)])


def _add_ner_entity(cfg_path: Path, name: str):
  """Insert an NER-style entity directly so merge has a victim to absorb."""
  conn = open_db(get_db_path(load_config(str(cfg_path))))
  try:
    conn.execute(
      "INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
      "VALUES (?, 'org', '[]', 1)",
      (name,),
    )
    conn.commit()
  finally:
    conn.close()


def test_merge_is_durable_in_config_and_db(tmp_path: Path):
  cfg = _config(tmp_path)
  _add_ner_entity(cfg, "Crayon AS")
  _add_ner_entity(cfg, "Crayon Group")

  result = _run(cfg, "entities", "merge", "Crayon", "Crayon AS", "Crayon Group", "--json")
  assert result.exit_code == 0, result.output
  stats = json.loads(result.output)["stats"]
  assert stats["victims"] == 2

  # Victims are gone from the DB...
  conn = open_db(get_db_path(load_config(str(cfg))), readonly=True)
  try:
    assert resolve_entity_id(conn, "Crayon AS") is None
    assert resolve_entity_id(conn, "Crayon Group") is None
    assert resolve_entity_id(conn, "Crayon") is not None
  finally:
    conn.close()

  # ...and durably folded into the config dictionary as aliases, so a reseed
  # (init-db / ingest) cannot resurrect them and NER will resolve them.
  doc = yaml.safe_load(Path(cfg).read_text())
  entries = {e["canonical"]: e for e in doc["entities"]["dictionary"]}
  assert "Crayon AS" not in entries and "Crayon Group" not in entries
  aliases = {a.lower() for a in entries["Crayon"].get("aliases", [])}
  assert {"crayon as", "crayon group"} <= aliases

  # Reseed and confirm the victims stay gone.
  assert _run(cfg, "init-db").exit_code == 0
  conn = open_db(get_db_path(load_config(str(cfg))), readonly=True)
  try:
    assert resolve_entity_id(conn, "Crayon AS") is None
  finally:
    conn.close()


def test_suggest_merges_groups_org_suffix_variants(tmp_path: Path):
  cfg = _config(tmp_path)
  _add_ner_entity(cfg, "Crayon AS")
  _add_ner_entity(cfg, "Crayon Consulting")
  _add_ner_entity(cfg, "Unrelated Org")

  result = _run(cfg, "entities", "suggest-merges", "--min-items", "0", "--json")
  assert result.exit_code == 0, result.output
  suggestions = json.loads(result.output)["suggestions"]
  crayon = [s for s in suggestions if s["survivor"].lower().startswith("crayon")]
  assert crayon, suggestions
  members = {m["name"] for m in crayon[0]["members"]}
  assert {"Crayon", "Crayon AS", "Crayon Consulting"} <= members
  # Unrelated entity is not dragged in.
  assert "Unrelated Org" not in members


def test_merge_unknown_entity_is_user_error(tmp_path: Path):
  cfg = _config(tmp_path)
  result = _run(cfg, "entities", "merge", "Crayon", "Ghost", "--json")
  assert result.exit_code != 0
  assert json.loads(result.output)["ok"] is False
