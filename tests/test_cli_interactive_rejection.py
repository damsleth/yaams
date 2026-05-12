"""Interactive commands MUST reject --json with a clear error.

Per mnem CONVENTIONS.md output-classes: interactive commands have
no machine path; consumers that pass --json get an actionable error
and exit 1, not a silent flag drop.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli


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


def _cfg(tmp_path: Path) -> Path:
  p = tmp_path / "config.yaml"
  p.write_text(_MIN.format(db_path=tmp_path / "data.db"))
  return p


def test_promote_review_rejects_json(tmp_path):
  cfg = _cfg(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(cli, ["promote", "review", "--config", str(cfg), "--json"])
  assert result.exit_code == 1
  assert "interactive" in (result.output + (result.stderr_bytes or b"").decode("utf-8", "ignore")).lower() \
    or "interactive" in result.output.lower()


def test_entities_manage_rejects_json(tmp_path):
  cfg = _cfg(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(cli, ["entities", "manage", "--config", str(cfg), "--json"])
  assert result.exit_code == 1
