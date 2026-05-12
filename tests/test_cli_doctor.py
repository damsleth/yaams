"""`yaams --doctor` - data-class health check.

Pins the mnem CONVENTIONS.md doctor schema for YAAMS.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams import __version__
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


def _write_config(tmp_path: Path, db_path: Path | None = None) -> Path:
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_MINIMAL_CONFIG.format(db_path=db_path or tmp_path / "data.db"))
  return cfg


def test_doctor_json_shape(tmp_path):
  cfg_path = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["--doctor", "--config", str(cfg_path), "--json"])
  payload = json.loads(result.output.strip())
  assert payload["tool"] == "yaams"
  assert payload["version"] == __version__
  assert payload["config_path"] == str(cfg_path)
  assert payload["data_path"] == str(tmp_path / "data.db")
  assert isinstance(payload["findings"], list)


def test_doctor_warns_on_missing_db(tmp_path):
  cfg_path = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["--doctor", "--config", str(cfg_path), "--json"])
  payload = json.loads(result.output.strip())
  ids = [f["id"] for f in payload["findings"]]
  assert "db_missing" in ids
  finding = next(f for f in payload["findings"] if f["id"] == "db_missing")
  assert finding["severity"] == "warning"
  assert "init-db" in finding["hint"]


def test_doctor_exit_0_on_warnings_only(tmp_path):
  cfg_path = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["--doctor", "--config", str(cfg_path), "--json"])
  # db_missing is a warning, not an error - exit code stays 0.
  assert result.exit_code == 0


def test_doctor_exit_1_on_missing_config(tmp_path):
  result = CliRunner().invoke(
    cli,
    ["--doctor", "--config", str(tmp_path / "absent.yaml"), "--json"],
  )
  assert result.exit_code != 0
  payload = json.loads(result.output.strip())
  ids = [f["id"] for f in payload["findings"]]
  # Either "config_missing" (file not at the explicit path) or
  # "config_unreadable" - both are acceptable failures here.
  assert any(s in ids for s in ("config_missing", "config_unreadable"))


def test_doctor_human_default(tmp_path):
  cfg_path = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["--doctor", "--config", str(cfg_path)])
  assert result.exit_code == 0, result.output
  assert "yaams doctor" in result.output
  assert "db_missing" in result.output


def test_doctor_redaction_sentinel_check_passes(tmp_path):
  """The doctor smoke test must NOT produce a 'redact_sentinel_leak' finding."""
  cfg_path = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["--doctor", "--config", str(cfg_path), "--json"])
  payload = json.loads(result.output.strip())
  ids = [f["id"] for f in payload["findings"]]
  assert "redact_sentinel_leak" not in ids


def test_doctor_reserved_key_compliance(tmp_path):
  """Doctor output is data class. Reserved-key contract requires no
  top-level `ok` on data success documents."""
  cfg_path = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["--doctor", "--config", str(cfg_path), "--json"])
  payload = json.loads(result.output.strip())
  assert "ok" not in payload
