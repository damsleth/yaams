"""Config alias rewriting per mnem CONVENTIONS.md.

Pins the `ingest.ledger:` -> `ingest.tier2_ledger:` rename. The
internal source id stays `tier2_ledger`; the alias is a CLI-and-
config friendliness only.
"""
from __future__ import annotations

from pathlib import Path

from yaams.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
  p = tmp_path / "config.yaml"
  p.write_text(body)
  return p


def test_ingest_ledger_alias_resolves_to_tier2_ledger(tmp_path):
  cfg = _write(tmp_path, """
db_path: /tmp/x.db
ingest:
  since: '2025-01-01T00:00:00Z'
  ledger:
    enabled: true
    notes_path: ~/notes
""")
  data = load_config(cfg)
  assert "ledger" not in data["ingest"]
  assert data["ingest"]["tier2_ledger"]["enabled"] is True
  assert data["ingest"]["tier2_ledger"]["notes_path"] == "~/notes"


def test_canonical_tier2_ledger_wins_over_alias(tmp_path):
  cfg = _write(tmp_path, """
db_path: /tmp/x.db
ingest:
  since: '2025-01-01T00:00:00Z'
  ledger:
    enabled: false
    notes_path: ~/wrong
  tier2_ledger:
    enabled: true
    notes_path: ~/right
""")
  data = load_config(cfg)
  assert data["ingest"]["tier2_ledger"]["enabled"] is True
  assert data["ingest"]["tier2_ledger"]["notes_path"] == "~/right"
  assert "ledger" not in data["ingest"]


def test_no_ingest_block_is_safe(tmp_path):
  cfg = _write(tmp_path, """
db_path: /tmp/x.db
""")
  data = load_config(cfg)
  assert "ingest" not in data or not data["ingest"]


def test_no_ledger_alias_passes_through_unchanged(tmp_path):
  cfg = _write(tmp_path, """
db_path: /tmp/x.db
ingest:
  since: '2025-01-01T00:00:00Z'
  imessage:
    enabled: false
""")
  data = load_config(cfg)
  assert data["ingest"]["imessage"]["enabled"] is False
  assert "tier2_ledger" not in data["ingest"]
