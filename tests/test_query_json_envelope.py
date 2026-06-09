"""JSON failure envelopes for `yaams query --json` (Plan 06).

Pins the data-class failure contract from the YAAMS CLI conventions:

- stdout is exactly ONE line of valid JSON
- ``ok`` is ``false``
- ``error.code`` is a stable identifier (config_not_found,
  config_invalid, db_open_failed)
- exit code maps to the YAAMS exit codes (4 for not-found, 1 for
  user error)
- tracebacks go to stderr, never stdout
"""
from __future__ import annotations

import json

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
  """Assert stdout is exactly one valid JSON object and return it."""
  lines = [ln for ln in output.splitlines() if ln.strip()]
  assert len(lines) == 1, f"expected one stdout line, got {len(lines)}: {output!r}"
  return json.loads(lines[0])


def test_query_missing_config_emits_envelope(tmp_path):
  runner = CliRunner()
  result = runner.invoke(
    cli,
    ["query", "hello", "--json", "--config", str(tmp_path / "missing.yaml")],
  )
  payload = _stdout_one_json_line(result.stdout)
  assert payload["tool"] == "yaams"
  assert payload["command"] == "query"
  assert payload["ok"] is False
  assert payload["error"]["code"] == "config_not_found"
  assert "hint" in payload["error"]
  assert result.exit_code == 4  # EXIT_NOT_FOUND


def test_query_malformed_config_emits_envelope(tmp_path):
  bad = tmp_path / "bad.yaml"
  bad.write_text("this: is: not: valid: yaml: [unclosed\n")
  runner = CliRunner()
  result = runner.invoke(
    cli, ["query", "hello", "--json", "--config", str(bad)],
  )
  payload = _stdout_one_json_line(result.stdout)
  assert payload["ok"] is False
  assert payload["error"]["code"] == "config_invalid"
  assert result.exit_code == 1  # EXIT_USER_ERROR


def test_query_missing_db_emits_envelope(tmp_path):
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_VALID_MINIMAL.format(db_path=tmp_path / "nope.db"))
  runner = CliRunner()
  result = runner.invoke(
    cli, ["query", "hello", "--json", "--config", str(cfg)],
  )
  payload = _stdout_one_json_line(result.stdout)
  assert payload["ok"] is False
  # ``open_db`` raises sqlite3.OperationalError or FileNotFoundError
  # depending on the SQLite path; both map to db_open_failed.
  assert payload["error"]["code"] == "db_open_failed"
  assert result.exit_code in (1, 4)


def test_query_traceback_only_on_stderr(tmp_path):
  """Stdout must remain pure JSON even when the body would traceback.

  Callers treat stdout as the result channel only; any bleed of stack
  frames would corrupt the envelope.
  """
  runner = CliRunner()
  result = runner.invoke(
    cli,
    ["query", "hello", "--json", "--config", str(tmp_path / "missing.yaml")],
  )
  # stdout: exactly one JSON line (the helper enforces this)
  _stdout_one_json_line(result.stdout)
  # stderr: contains the traceback for debug purposes
  assert "Traceback" in result.stderr
  assert "FileNotFoundError" in result.stderr


def test_query_no_json_flag_still_tracebacks(tmp_path):
  """Without --json the guard is a no-op: human mode keeps the traceback."""
  runner = CliRunner()
  result = runner.invoke(
    cli, ["query", "hello", "--config", str(tmp_path / "missing.yaml")],
  )
  # stdout should NOT contain a JSON envelope; the guard yields without
  # catching. Click surfaces the exception in stderr.
  assert result.exit_code != 0
  # Sanity: nothing pretending to be a success envelope leaks to stdout.
  if result.stdout.strip():
    try:
      payload = json.loads(result.stdout.strip().splitlines()[0])
      # If we did emit JSON, it must not falsely claim ok=true.
      assert payload.get("ok") is not True
    except json.JSONDecodeError:
      pass  # not JSON, which is the expected human-mode shape
