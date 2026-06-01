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
    - canonical: Norconsult
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


def test_tag_set_show_roundtrip(tmp_path: Path):
  cfg = _config(tmp_path)
  assert _run(cfg, "entities", "tag", "Norconsult", "customer", "defense").exit_code == 0
  assert _run(cfg, "entities", "set", "Norconsult", "sector=public", "region=oslo").exit_code == 0
  show = _run(cfg, "entities", "show", "Norconsult", "--json")
  assert show.exit_code == 0, show.output
  doc = json.loads(show.output)
  assert set(doc["tags"]) == {"customer", "defense"}
  assert doc["meta"] == {"sector": "public", "region": "oslo"}


def test_set_rejects_bad_attribute(tmp_path: Path):
  cfg = _config(tmp_path)
  result = _run(cfg, "entities", "set", "Norconsult", "noequalsign", "--json")
  assert result.exit_code != 0
  assert json.loads(result.output)["ok"] is False


def test_untag_and_unset(tmp_path: Path):
  cfg = _config(tmp_path)
  _run(cfg, "entities", "tag", "Norconsult", "customer", "defense")
  _run(cfg, "entities", "set", "Norconsult", "sector=public")
  assert _run(cfg, "entities", "untag", "Norconsult", "defense").exit_code == 0
  assert _run(cfg, "entities", "unset", "Norconsult", "sector").exit_code == 0
  doc = json.loads(_run(cfg, "entities", "show", "Norconsult", "--json").output)
  assert doc["tags"] == ["customer"]
  assert doc["meta"] == {}


def test_tag_unknown_entity_is_user_error(tmp_path: Path):
  cfg = _config(tmp_path)
  result = _run(cfg, "entities", "tag", "Nope", "customer", "--json")
  assert result.exit_code != 0
  assert json.loads(result.output)["ok"] is False
