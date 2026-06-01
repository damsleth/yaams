from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli

_CONFIG = """
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'

embed:
  model: dummy
  dimension: 4

entities:
  dictionary:
    - canonical: fdep
      type: org
    - canonical: langkaia
      type: place

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


def test_assoc_link_rejects_weight_above_one(tmp_path: Path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(
    cli, ["assoc", "link", "fdep", "langkaia", "--weight", "1.5", "--config", str(cfg)]
  )
  assert result.exit_code != 0
  assert "1.5" in result.output or "range" in result.output.lower()


def test_assoc_link_rejects_zero_weight(tmp_path: Path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(
    cli, ["assoc", "link", "fdep", "langkaia", "--weight", "0", "--config", str(cfg)]
  )
  assert result.exit_code != 0


def test_assoc_link_show_roundtrip(tmp_path: Path):
  cfg = _config(tmp_path)
  link = CliRunner().invoke(
    cli,
    ["assoc", "link", "fdep", "langkaia", "--weight", "0.7", "--both", "--config", str(cfg)],
  )
  assert link.exit_code == 0, link.output
  show = CliRunner().invoke(
    cli, ["assoc", "show", "fdep", "--json", "--config", str(cfg)]
  )
  assert show.exit_code == 0, show.output
  doc = json.loads(show.output)
  assocs = {a["entity"]: a["weight"] for a in doc["associations"]}
  assert assocs.get("langkaia") == 0.7


def test_assoc_show_unknown_entity_is_user_error(tmp_path: Path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(
    cli, ["assoc", "show", "nope", "--json", "--config", str(cfg)]
  )
  assert result.exit_code != 0
  assert json.loads(result.output)["ok"] is False
