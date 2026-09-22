"""`sources_context`: owner-written one-liners returned alongside hits."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from yaams.cli import sources as sources_mod
from yaams.config import load_config, source_context_for
from yaams.mcp.server import _results_payload
from yaams.retrieve import HybridResult
from yaams.synthesize import build_synthesis_prompt

_CFG = {
  "sources_context": {
    "teams_brkh": "BRKH volunteer fire brigade Teams; vakt = shift roster",
    "teams": "Microsoft Teams chats, one source per profile",
    "imessage": "Personal iMessage",
  },
}


def _result(item_id: str, source: str) -> HybridResult:
  return HybridResult(
    id=item_id,
    kind="item",
    source=source,
    timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    sender="a@test",
    subject="",
    content="hello",
    thread_id=None,
    score=0.5,
  )


def test_payload_has_context_for_present_sources_only():
  payload = _results_payload([_result("1", "teams_brkh"), _result("2", "teams_brkh")], _CFG)
  assert payload["context"] == {"teams_brkh": _CFG["sources_context"]["teams_brkh"]}
  assert "imessage" not in payload["context"]


def test_family_key_falls_back_for_profile_sources():
  ctx = source_context_for(_CFG, ["teams_swon", "teams_channels_swon", "teams_brkh"])
  assert ctx == {
    "teams_swon": _CFG["sources_context"]["teams"],
    "teams_channels_swon": _CFG["sources_context"]["teams"],
    "teams_brkh": _CFG["sources_context"]["teams_brkh"],
  }


def test_unknown_source_and_empty_config_yield_no_key():
  assert source_context_for(_CFG, ["email"]) == {}
  assert "context" not in _results_payload([_result("1", "email")], _CFG)
  assert "context" not in _results_payload([_result("1", "teams_brkh")], {})
  assert "context" not in _results_payload([_result("1", "teams_brkh")], {"sources_context": None})


def test_synthesis_prompt_prepends_source_notes():
  notes = {"teams_brkh": "BRKH {brigade}: vakt = shift"}
  prompt = build_synthesis_prompt("q", [_result("1", "teams_brkh")], source_notes=notes)
  assert prompt.startswith("Source notes")
  assert "- teams_brkh: BRKH {brigade}: vakt = shift" in prompt
  assert "Source notes" not in build_synthesis_prompt("q", [_result("1", "teams_brkh")])


def test_yaml_set_source_context_round_trips(tmp_path: Path):
  cfg = tmp_path / "config.yaml"
  cfg.write_text("db_path: /tmp/x.db\ningest:\n  since: '2025-01-01T00:00:00Z'\n")
  sources_mod._yaml_set_source_context(cfg, "teams_brkh", 'BRKH: fire brigade # "vakt"')
  sources_mod._yaml_set_source_context(cfg, "teams", "family note")
  sources_mod._yaml_set_source_context(cfg, "teams_brkh", "replaced")
  loaded = load_config(cfg)
  assert loaded["sources_context"] == {"teams_brkh": "replaced", "teams": "family note"}
  assert loaded["ingest"]["since"] == "2025-01-01T00:00:00Z"


def test_context_key_per_row_kind():
  assert sources_mod._context_key(
    sources_mod.SourceRow(kind="source", name="teams", enabled=True, summary="")
  ) == "teams"
  assert sources_mod._context_key(sources_mod.SubPathRow(
    kind="subpath", parent="teams", subkind="profile", index=0, label="brkh", enabled=True,
  )) == "teams_brkh"
  assert sources_mod._context_key(sources_mod.SubPathRow(
    kind="subpath", parent="folders", subkind="path", index=0, label="~/x", enabled=True,
  )) == "folders"
