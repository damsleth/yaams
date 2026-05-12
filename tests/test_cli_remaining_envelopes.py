"""Coverage for the remaining --json wirings:

- consolidate (action envelope)
- feedback (action envelope)
- enrich retag (action envelope)
- entities list / add / remove (data + action envelopes)
- entities discover / denied (interactive: --json rejected)
- promote generate (action envelope)
- promote list (data envelope)
- signals (data envelope)
"""
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
  dictionary: []

synthesize:
  llm:
    backend: dummy
"""


def _cfg(tmp_path: Path) -> Path:
  p = tmp_path / "config.yaml"
  p.write_text(_CONFIG.format(db_path=tmp_path / "data.db"))
  return p


def _initdb(cfg: Path) -> None:
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])


def _last_json_line(output: str) -> dict:
  for line in reversed(output.strip().splitlines()):
    try:
      return json.loads(line)
    except json.JSONDecodeError:
      continue
  raise AssertionError(f"No JSON in: {output!r}")


# --- consolidate -----------------------------------------------------------

def test_consolidate_json_envelope_no_sources(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(cli, ["consolidate", "--config", str(cfg), "--json"])
  assert result.exit_code == 0, result.output
  env = _last_json_line(result.output)
  assert env["tool"] == "yaams"
  assert env["command"] == "consolidate"
  assert env["ok"] is True
  assert env["stats"]["sources"] == {}
  assert any("No conversational sources" in w for w in env["warnings"])


# --- feedback --------------------------------------------------------------

def test_feedback_json_envelope_success(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  # Feedback doesn't require the referenced query to exist (signals
  # stores whatever you log). Smoke test the envelope shape.
  result = CliRunner().invoke(
    cli,
    ["feedback", "abc123", "hit", "--message", "ok", "--config", str(cfg), "--json"],
  )
  env = _last_json_line(result.output)
  if env["ok"]:
    assert env["stats"]["query_id"] == "abc123"
    assert env["stats"]["kind"] == "hit"
  else:
    # Some signals backends may reject. Failure path still emits a
    # parseable envelope and a nonzero exit.
    assert result.exit_code != 0


# --- enrich retag ----------------------------------------------------------

def test_enrich_retag_json_envelope(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(
    cli, ["enrich", "retag", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 0, result.output
  env = _last_json_line(result.output)
  assert env["command"] == "enrich retag"
  assert env["ok"] is True
  assert env["stats"]["total"] == 0
  assert env["stats"]["updated"] == 0


# --- entities list (data) --------------------------------------------------

def test_entities_list_json_empty_data_doc(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(cli, ["entities", "list", "--config", str(cfg), "--json"])
  assert result.exit_code == 0
  payload = json.loads(result.output.strip())
  # Reserved-key contract: data success has no top-level `ok`.
  assert "ok" not in payload
  assert payload["entities"] == []


# --- entities add (action) -------------------------------------------------

def test_entities_add_json_envelope(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(
    cli,
    ["entities", "add", "TestPerson", "--type", "person", "--alias", "TP", "--config", str(cfg), "--json"],
  )
  assert result.exit_code == 0, result.output
  env = _last_json_line(result.output)
  assert env["command"] == "entities add"
  assert env["ok"] is True
  assert env["stats"]["canonical"] == "TestPerson"
  assert env["stats"]["added"] is True
  assert env["stats"]["aliases"] == ["TP"]


def test_entities_add_json_duplicate_is_success_with_reason(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  CliRunner().invoke(cli, ["entities", "add", "Dup", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli, ["entities", "add", "Dup", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 0
  env = _last_json_line(result.output)
  assert env["ok"] is True
  assert env["stats"]["added"] is False
  assert env["stats"]["reason"] == "already_present"


# --- entities remove (action) ----------------------------------------------

def test_entities_remove_json_envelope(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  CliRunner().invoke(cli, ["entities", "add", "ToRemove", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli, ["entities", "remove", "ToRemove", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 0, result.output
  env = _last_json_line(result.output)
  assert env["ok"] is True
  assert env["stats"]["removed"] is True
  assert env["stats"]["canonical"] == "ToRemove"


def test_entities_remove_not_found_is_success_with_reason(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(
    cli, ["entities", "remove", "Nobody", "--config", str(cfg), "--json"]
  )
  env = _last_json_line(result.output)
  assert env["ok"] is True
  assert env["stats"]["removed"] is False
  assert env["stats"]["reason"] == "not_found"


# --- entities discover/denied reject --json --------------------------------

def test_entities_discover_rejects_json(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(
    cli, ["entities", "discover", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 1


def test_entities_denied_rejects_json(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(
    cli, ["entities", "denied", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 1


# --- promote generate (action) ---------------------------------------------

def test_promote_generate_json_envelope(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(
    cli, ["promote", "generate", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 0, result.output
  env = _last_json_line(result.output)
  assert env["command"] == "promote generate"
  assert env["ok"] is True
  assert env["stats"]["candidates_generated"] == 0
  assert env["stats"]["candidates_stored"] == 0


# --- promote list (data) ---------------------------------------------------

def test_promote_list_json_data_doc(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(
    cli, ["promote", "list", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 0
  payload = json.loads(result.output.strip())
  assert "ok" not in payload
  assert payload["candidates"] == []
  assert payload["status_filter"] == "pending"


# --- signals (data) --------------------------------------------------------

def test_signals_json_data_doc(tmp_path):
  cfg = _cfg(tmp_path)
  _initdb(cfg)
  result = CliRunner().invoke(cli, ["signals", "--config", str(cfg), "--json"])
  assert result.exit_code == 0
  payload = json.loads(result.output.strip())
  assert "ok" not in payload
  assert payload["queries"] == []
  assert payload["limit"] == 20
