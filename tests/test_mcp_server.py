"""YAAMS MCP server: egress scrub, tool registration, write-gating."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from yaams.cli import cli
from yaams.mcp.server import create_server, scrub_for_egress

pytest.importorskip("mcp")

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
trust:
  show_trust_verdict: true
"""


def _config(tmp_path: Path) -> Path:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG.format(db_path=db))
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  return cfg


def _tool_names(server) -> set[str]:
  return {t.name for t in server._tool_manager.list_tools()}


def test_scrub_for_egress_strips_private():
  assert scrub_for_egress("a<private>secret</private>b") == "ab"
  nested = {"k": ["x<private>y</private>z"], "n": {"m": "<private>q</private>!"}}
  assert scrub_for_egress(nested) == {"k": ["xz"], "n": {"m": "!"}}


def test_scrub_for_egress_passthrough():
  assert scrub_for_egress({"a": 1, "b": [2, 3]}) == {"a": 1, "b": [2, 3]}


def test_read_tools_registered(tmp_path):
  cfg = _config(tmp_path)
  server = create_server(config_path=str(cfg), allow_write=False)
  names = _tool_names(server)
  assert "yaams_query" in names
  assert "yaams_answer" in names


def test_feedback_tool_gated_off_by_default(tmp_path):
  cfg = _config(tmp_path)
  server = create_server(config_path=str(cfg), allow_write=False)
  assert "yaams_feedback" not in _tool_names(server)


def test_feedback_tool_present_with_allow_write(tmp_path):
  cfg = _config(tmp_path)
  server = create_server(config_path=str(cfg), allow_write=True)
  assert "yaams_feedback" in _tool_names(server)


def test_mcp_command_registered():
  assert "mcp" in cli.commands


def test_log_mcp_query_sets_mcp_provenance(tmp_path):
  from yaams.config import get_db_path, load_config
  from yaams.db import open_db
  from yaams.mcp.server import _log_mcp_query

  cfg_path = _config(tmp_path)
  cfg = load_config(str(cfg_path))
  _log_mcp_query(
    cfg, query_id="q_mcp", text="what did we decide", top_k=5,
    source_filter=None, results=[], latency_ms=1.0, retrieval_ms=1.0,
  )
  conn = open_db(get_db_path(cfg))
  try:
    row = conn.execute(
      "SELECT provenance, text FROM queries WHERE id = 'q_mcp'"
    ).fetchone()
  finally:
    conn.close()
  assert row is not None
  assert row["provenance"] == "mcp"
  assert row["text"] == "what did we decide"


def test_feedback_boost_flag_default_off(tmp_path):
  from yaams.config import load_config
  from yaams.mcp.server import _feedback_boost

  cfg = load_config(str(_config(tmp_path)))
  assert _feedback_boost(cfg) is False
  cfg["retrieve"] = {"feedback_boost": True}
  assert _feedback_boost(cfg) is True


def test_answer_token_budget_cuts_tail_keeps_rank_one():
  from datetime import UTC, datetime

  from yaams.mcp.server import _apply_token_budget
  from yaams.retrieve.hybrid import HybridResult

  def res(i: int, chars: int) -> HybridResult:
    return HybridResult(
      id=f"r{i}", kind="item", source="email", timestamp=datetime(2026, 1, 1, tzinfo=UTC),
      sender="", subject="", content="x" * chars, thread_id=None, score=1.0 / i,
    )

  results = [res(1, 400), res(2, 400), res(3, 400)]
  assert _apply_token_budget(results, 0) == (results, None)
  kept, omitted = _apply_token_budget(results, 250)
  assert [r.id for r in kept] == ["r1", "r2"]
  assert omitted == {"count": 1, "from_rank": 3, "reason": "budget"}

  kept, omitted = _apply_token_budget(results, 50)
  assert [r.id for r in kept] == ["r1"]
  assert kept[0].content.endswith("[truncated]") and len(kept[0].content) < 400
  assert results[0].content == "x" * 400
  assert omitted == {"count": 2, "from_rank": 2, "reason": "budget"}
