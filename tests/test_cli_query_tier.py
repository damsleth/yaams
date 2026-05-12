"""`yaams query` Phase 2b additions.

Pins the mnem CONVENTIONS.md contract for the query data command:

- `--json` flag as a machine-mode alias for `--format json`.
- `--pretty` flag as the human-mode alias for `--format text`.
- `--tier raw|ledger|both` translating to source filters.
- `--source ledger` CLI alias for the internal `tier2_ledger` source id.
- Reserved-key contract: the success JSON document has no top-level `ok`.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli
from yaams.cli.query import _LEDGER_SOURCE_ID, _resolve_source_filter

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


def _config(tmp_path: Path) -> Path:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG.format(db_path=db))
  return cfg


# --- unit tests of the source/tier resolver ---------------------------------

def test_resolve_source_filter_explicit_source_wins_over_tier():
  out = _resolve_source_filter(("imessage",), "ledger")
  assert out == ["imessage"]


def test_resolve_source_filter_ledger_alias():
  out = _resolve_source_filter(("ledger",), None)
  assert out == [_LEDGER_SOURCE_ID]


def test_resolve_source_filter_tier_ledger():
  out = _resolve_source_filter((), "ledger")
  assert out == [_LEDGER_SOURCE_ID]


def test_resolve_source_filter_tier_both_returns_none():
  out = _resolve_source_filter((), "both")
  assert out is None


def test_resolve_source_filter_tier_raw_returns_none():
  # tier=raw expresses an exclusion; the include-list resolver returns
  # None and the caller applies the post-filter step.
  out = _resolve_source_filter((), "raw")
  assert out is None


def test_resolve_source_filter_default():
  out = _resolve_source_filter((), None)
  assert out is None


def test_resolve_source_filter_multiple_sources_with_ledger_alias():
  out = _resolve_source_filter(("imessage", "ledger"), None)
  assert out == ["imessage", _LEDGER_SOURCE_ID]


# --- integration tests through the CLI -------------------------------------

def test_query_json_flag_is_alias_for_format_json(tmp_path):
  cfg = _config(tmp_path)
  # init-db so query has a real (empty) schema to read.
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--json", "anything"],
  )
  assert result.exit_code == 0, result.output
  payload = json.loads(result.output.strip())
  assert payload["question"] == "anything"
  assert payload["results"] == []


def test_query_pretty_flag_is_alias_for_format_text(tmp_path):
  cfg = _config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--pretty", "anything"],
  )
  assert result.exit_code == 0
  # Human mode produces a "No results." message; never valid JSON.
  assert "No results" in result.output


def test_query_reserved_key_contract(tmp_path):
  """Reserved-key contract: data success has no top-level `ok`."""
  cfg = _config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--json", "anything"],
  )
  payload = json.loads(result.output.strip())
  assert "ok" not in payload


def test_query_tier_ledger_smoke(tmp_path):
  """tier=ledger restricts to tier2_ledger source. Smoke test - the
  store is empty, but the flag must be accepted without error."""
  cfg = _config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--tier", "ledger", "--json", "x"],
  )
  assert result.exit_code == 0
  payload = json.loads(result.output.strip())
  assert payload["results"] == []


def test_query_tier_raw_smoke(tmp_path):
  cfg = _config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--tier", "raw", "--json", "x"],
  )
  assert result.exit_code == 0


def test_query_tier_both_default(tmp_path):
  cfg = _config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--tier", "both", "--json", "x"],
  )
  assert result.exit_code == 0


def test_query_legacy_format_json_still_works(tmp_path):
  cfg = _config(tmp_path)
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--format", "json", "x"],
  )
  assert result.exit_code == 0
  json.loads(result.output.strip())
