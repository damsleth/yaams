"""Action envelopes on data/action class commands.

Pins the mnem CLI contract from mnem/CONVENTIONS.md for the simple
commands in yaams/cli/main.py: init-db, setup, stats, reset-db.
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


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_MINIMAL_CONFIG.format(db_path=db))
  return cfg, db


def _last_json_line(output: str) -> dict:
  """Action envelopes are one-line JSON. Pick the last parseable line."""
  for line in reversed(output.strip().splitlines()):
    try:
      return json.loads(line)
    except json.JSONDecodeError:
      continue
  raise AssertionError(f"No JSON line in output: {output!r}")


# --- init-db -----------------------------------------------------------------

def test_init_db_json_envelope_on_success(tmp_path):
  cfg_path, db_path = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["init-db", "--config", str(cfg_path), "--json"])
  assert result.exit_code == 0, result.output
  env = _last_json_line(result.output)
  assert env["tool"] == "yaams"
  assert env["version"] == __version__
  assert env["command"] == "init-db"
  assert env["ok"] is True
  assert env["error"] is None
  assert env["stats"]["db_path"] == str(db_path)
  assert env["stats"]["created"] is True
  assert isinstance(env["duration_ms"], (int, float))


def test_init_db_human_default_unchanged(tmp_path):
  cfg_path, _ = _write_config(tmp_path)
  result = CliRunner().invoke(cli, ["init-db", "--config", str(cfg_path)])
  assert result.exit_code == 0, result.output
  assert "Initialized database" in result.output


def test_init_db_json_envelope_on_failure(tmp_path):
  # Point at an invalid config path; init should fail and emit the
  # error envelope.
  result = CliRunner().invoke(cli, ["init-db", "--config", str(tmp_path / "nope.yaml"), "--json"])
  assert result.exit_code != 0
  env = _last_json_line(result.output)
  assert env["ok"] is False
  assert env["error"]["code"]
  assert env["error"]["message"]


# --- stats -------------------------------------------------------------------

def test_stats_json_emits_raw_data_doc(tmp_path):
  cfg_path, db_path = _write_config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg_path)])
  result = CliRunner().invoke(cli, ["stats", "--config", str(cfg_path), "--json"])
  assert result.exit_code == 0, result.output
  payload = json.loads(result.output.strip())
  # Reserved-key contract: data success documents MUST NOT have a
  # top-level `ok`.
  assert "ok" not in payload
  assert payload["db_path"] == str(db_path)
  assert "by_source" in payload
  assert payload["total"] == 0


def test_stats_json_failure_envelope_when_db_missing(tmp_path):
  cfg_path, _ = _write_config(tmp_path)
  # Don't init-db; opening readonly should fail.
  result = CliRunner().invoke(cli, ["stats", "--config", str(cfg_path), "--json"])
  # Exit nonzero, ok=false in envelope (data-class failure).
  assert result.exit_code != 0
  env = _last_json_line(result.output)
  assert env["ok"] is False
  assert env["error"]["code"]


# --- reset-db ----------------------------------------------------------------

def test_reset_db_json_envelope_on_success(tmp_path):
  cfg_path, db_path = _write_config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg_path)])
  assert db_path.exists()
  result = CliRunner().invoke(cli, ["reset-db", "--config", str(cfg_path), "--yes", "--json"])
  assert result.exit_code == 0, result.output
  env = _last_json_line(result.output)
  assert env["command"] == "reset-db"
  assert env["ok"] is True
  assert env["stats"]["db_path"] == str(db_path)
  assert env["stats"]["removed"] is True
  assert not db_path.exists()


def test_reset_db_json_requires_yes_when_not_tty(tmp_path):
  cfg_path, _ = _write_config(tmp_path)
  # CliRunner is non-TTY by default. --json without --yes must refuse.
  result = CliRunner().invoke(cli, ["reset-db", "--config", str(cfg_path), "--json"])
  assert result.exit_code != 0
  env = _last_json_line(result.output)
  assert env["ok"] is False
  assert env["error"]["code"] == "confirmation_required"


def test_reset_db_human_unchanged_with_yes(tmp_path):
  cfg_path, db_path = _write_config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg_path)])
  result = CliRunner().invoke(cli, ["reset-db", "--config", str(cfg_path), "--yes"])
  assert result.exit_code == 0, result.output
  assert "Removed database" in result.output
