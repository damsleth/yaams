"""`yaams ingest --json` - action envelope + NDJSON streaming.

Pins the mnem CONVENTIONS.md contract for the most complex action
command: streaming progress lines on stdout, a final result envelope,
and the partial-success exit-code rules (0 / 1 / 5).
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams import __version__
from yaams.cli import cli


_CONFIG_NO_SOURCES = """
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'
  imessage:
    enabled: false
    chat_db_path: ~/none
  signal:
    enabled: false
  email:
    enabled: false
  notes:
    enabled: false
  tier2_ledger:
    enabled: false
  github:
    enabled: false
    username: ''

embed:
  model: dummy
  dimension: 4

entities:
  dictionary: []
"""


def _config(tmp_path: Path) -> Path:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG_NO_SOURCES.format(db_path=db))
  return cfg


def _parse_ndjson(output: str) -> list[dict]:
  lines = []
  for raw in output.strip().splitlines():
    raw = raw.strip()
    if not raw:
      continue
    try:
      lines.append(json.loads(raw))
    except json.JSONDecodeError:
      continue
  return lines


def test_ingest_json_emits_result_envelope_when_no_sources(tmp_path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(cli, ["ingest", "--config", str(cfg), "--json"])
  assert result.exit_code == 0, result.output
  events = _parse_ndjson(result.output)
  assert any(e.get("type") == "result" for e in events)
  final = next(e for e in events if e.get("type") == "result")
  assert final["tool"] == "yaams"
  assert final["version"] == __version__
  assert final["command"] == "ingest"
  assert final["ok"] is True
  assert final["stats"]["sources_planned"] == []
  # Warning surface is populated when nothing is enabled.
  assert any("No sources enabled" in w for w in final.get("warnings", []))


def test_ingest_json_progress_plan_line(tmp_path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(cli, ["ingest", "--config", str(cfg), "--json"])
  events = _parse_ndjson(result.output)
  # First event must be a progress line declaring the plan size.
  plan = [e for e in events if e.get("type") == "progress" and e.get("stage") == "plan"]
  assert plan, result.output
  assert plan[0]["total"] == 0


def test_ingest_json_result_is_last_line(tmp_path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(cli, ["ingest", "--config", str(cfg), "--json"])
  events = _parse_ndjson(result.output)
  result_events = [e for e in events if e.get("type") == "result"]
  assert len(result_events) == 1
  # And it appears after all progress/warning lines.
  last = events[-1]
  assert last.get("type") == "result"


def test_ingest_json_envelope_invariant(tmp_path):
  """ok=true ⇔ exit 0 invariant."""
  cfg = _config(tmp_path)
  result = CliRunner().invoke(cli, ["ingest", "--config", str(cfg), "--json"])
  events = _parse_ndjson(result.output)
  final = next(e for e in events if e.get("type") == "result")
  assert final["ok"] is True
  assert result.exit_code == 0


def test_ingest_dry_run_with_json_still_emits_envelope(tmp_path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(
    cli, ["ingest", "--config", str(cfg), "--dry-run", "--json"]
  )
  assert result.exit_code == 0
  final = next(e for e in _parse_ndjson(result.output) if e.get("type") == "result")
  assert final["stats"]["dry_run"] is True


def test_ingest_human_default_unchanged(tmp_path):
  cfg = _config(tmp_path)
  result = CliRunner().invoke(cli, ["ingest", "--config", str(cfg)])
  assert result.exit_code == 0, result.output
  # Human path still prints the "stats" block to stdout.
  assert "Total in DB" in result.output


def test_strict_flag_present(tmp_path):
  """--strict is wired through; without failed sources it's a no-op."""
  cfg = _config(tmp_path)
  result = CliRunner().invoke(
    cli, ["ingest", "--config", str(cfg), "--json", "--strict"]
  )
  assert result.exit_code == 0
  final = next(e for e in _parse_ndjson(result.output) if e.get("type") == "result")
  assert final["ok"] is True


# --- Unit test of the exit-code builder (catches partial / all-failed cases
#     without needing real adapters to fail).

def test_build_envelope_all_succeeded(tmp_path):
  from yaams.cli.ingest import _build_ingest_envelope
  env, code = _build_ingest_envelope(
    run_stats={"imessage": {"seen": 10, "new": 10, "skipped": 0}},
    succeeded=["imessage"],
    failed_sources=[],
    sources_planned=["imessage"],
    dry_run=False,
    total_duration_ms=12.5,
    strict=False,
  )
  assert env["ok"] is True
  assert code == 0


def test_build_envelope_all_failed():
  from yaams.cli.ingest import _build_ingest_envelope
  env, code = _build_ingest_envelope(
    run_stats={"imessage": {"failed": "boom"}, "signal": {"failed": "boom"}},
    succeeded=[],
    failed_sources=["imessage", "signal"],
    sources_planned=["imessage", "signal"],
    dry_run=False,
    total_duration_ms=12.5,
    strict=False,
  )
  assert env["ok"] is False
  assert env["error"]["code"] == "all_sources_failed"
  assert sorted(env["error"]["failed_sources"]) == ["imessage", "signal"]
  assert code == 1


def test_build_envelope_partial_success_exits_5():
  from yaams.cli.ingest import _build_ingest_envelope
  env, code = _build_ingest_envelope(
    run_stats={
      "imessage": {"seen": 10, "new": 10, "skipped": 0},
      "signal": {"failed": "sqlcipher missing"},
    },
    succeeded=["imessage"],
    failed_sources=["signal"],
    sources_planned=["imessage", "signal"],
    dry_run=False,
    total_duration_ms=12.5,
    strict=False,
  )
  assert env["ok"] is False
  assert env["error"]["code"] == "partial_failure"
  assert env["error"]["failed_sources"] == ["signal"]
  assert code == 5


def test_build_envelope_partial_with_strict_exits_1():
  from yaams.cli.ingest import _build_ingest_envelope
  env, code = _build_ingest_envelope(
    run_stats={
      "imessage": {"seen": 10, "new": 10, "skipped": 0},
      "signal": {"failed": "sqlcipher missing"},
    },
    succeeded=["imessage"],
    failed_sources=["signal"],
    sources_planned=["imessage", "signal"],
    dry_run=False,
    total_duration_ms=12.5,
    strict=True,
  )
  assert env["ok"] is False
  assert env["error"]["code"] == "partial_failure_strict"
  assert code == 1
