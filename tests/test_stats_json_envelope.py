"""JSON failure envelopes for `yaams stats --json` (Plan 06).

Parallel coverage to test_query_json_envelope.py for the ``stats``
data command. Stats loads config and opens the SQLite db before doing
anything else; any failure must surface as a single-line data_error
envelope on stdout under --json.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli


_VALID_MINIMAL = """\
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'

embed:
  model: dummy
  dimension: 4

entities:
  dictionary: []
"""


def _stdout_one_json_line(output: str) -> dict:
  lines = [ln for ln in output.splitlines() if ln.strip()]
  assert len(lines) == 1, f"expected one stdout line, got {len(lines)}: {output!r}"
  return json.loads(lines[0])


def test_stats_missing_config_emits_envelope(tmp_path):
  runner = CliRunner()
  result = runner.invoke(
    cli, ["stats", "--json", "--config", str(tmp_path / "nope.yaml")],
  )
  payload = _stdout_one_json_line(result.stdout)
  assert payload["tool"] == "yaams"
  assert payload["command"] == "stats"
  assert payload["ok"] is False
  assert payload["error"]["code"] == "config_not_found"
  assert "hint" in payload["error"]
  assert result.exit_code == 4


def test_stats_malformed_config_emits_envelope(tmp_path):
  bad = tmp_path / "broken.yaml"
  bad.write_text(":\n- not: a: valid: mapping: [oops\n")
  runner = CliRunner()
  result = runner.invoke(
    cli, ["stats", "--json", "--config", str(bad)],
  )
  payload = _stdout_one_json_line(result.stdout)
  assert payload["ok"] is False
  assert payload["error"]["code"] == "config_invalid"
  assert result.exit_code == 1


def test_stats_missing_db_emits_envelope(tmp_path):
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_VALID_MINIMAL.format(db_path=tmp_path / "does-not-exist.db"))
  runner = CliRunner()
  result = runner.invoke(
    cli, ["stats", "--json", "--config", str(cfg)],
  )
  payload = _stdout_one_json_line(result.stdout)
  assert payload["ok"] is False
  assert payload["error"]["code"] == "db_open_failed"
  assert result.exit_code in (1, 4)


def test_stats_traceback_only_on_stderr(tmp_path):
  runner = CliRunner()
  result = runner.invoke(
    cli, ["stats", "--json", "--config", str(tmp_path / "nope.yaml")],
  )
  _stdout_one_json_line(result.stdout)
  assert "Traceback" in result.stderr
