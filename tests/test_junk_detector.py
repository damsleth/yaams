from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli
from yaams.cli.entities import _build_prune_candidates, _junk_reasons
from yaams.schema import init_schema


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _ent(conn, name, pending=1):
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
    "VALUES (?, 'org', '[]', ?)",
    (name, pending),
  )
  conn.commit()


# --- heuristic unit tests --------------------------------------------------

def test_junk_reasons_flags_stopword_and_lowercase():
  assert "stopword" in _junk_reasons("takk")
  assert "all-lowercase" in _junk_reasons("skjønner jeg")


def test_junk_reasons_flags_short_numeric_and_symbol():
  assert "very-short" in _junk_reasons("ah")
  assert "numeric" in _junk_reasons("2024")
  assert "symbol-heavy" in _junk_reasons("n++")


def test_junk_reasons_keeps_real_entities_clean():
  assert _junk_reasons("Crayon") == []
  assert _junk_reasons("Bærum Røde Kors") == []
  assert _junk_reasons("Jan Henning Peters") == []


def test_junk_reasons_keeps_uppercase_acronyms():
  # short but uppercase -> acronym, not "very-short" junk
  assert "very-short" not in _junk_reasons("EU")
  assert _junk_reasons("FN") == []


# --- candidate builder -----------------------------------------------------

def test_build_prune_candidates_excludes_curated_and_sorts_by_usage():
  conn = _open_db()
  _ent(conn, "takk")           # junk, ner
  _ent(conn, "ja")             # junk, ner
  _ent(conn, "Crayon")         # real, ner -> not flagged
  _ent(conn, "for", pending=0)  # curated dictionary entity -> excluded even if word-like

  cands = _build_prune_candidates(conn)
  names = [c["name"] for c in cands]
  assert "takk" in names and "ja" in names
  assert "Crayon" not in names
  assert "for" not in names  # curated entity never suggested for pruning


def test_build_prune_candidates_respects_max_items():
  conn = _open_db()
  _ent(conn, "takk")
  # give 'takk' some links so max-items can exclude it
  conn.execute("INSERT INTO items (id, source, source_id, timestamp, sender, recipients, content, ingested_at) "
               "VALUES ('i1','imessage','x','2026-01-01','a','[]','c','2026-01-01')")
  tid = conn.execute("SELECT id FROM entities WHERE canonical_name='takk'").fetchone()["id"]
  conn.execute("INSERT INTO item_entities (item_id, entity_id, source) VALUES ('i1', ?, 'ner')", (tid,))
  conn.commit()
  assert _build_prune_candidates(conn, max_items=0) == []  # 1 link > 0
  assert [c["name"] for c in _build_prune_candidates(conn, max_items=5)] == ["takk"]


# --- CLI -------------------------------------------------------------------

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


def test_suggest_prune_cli_json(tmp_path: Path):
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG.format(db_path=db))
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  conn = sqlite3.connect(db)
  conn.execute("INSERT INTO entities (canonical_name, entity_type, aliases, pending_review) "
               "VALUES ('takk','org','[]',1)")
  conn.commit()
  conn.close()

  result = CliRunner().invoke(cli, ["entities", "suggest-prune", "--json", "--config", str(cfg)])
  assert result.exit_code == 0, result.output
  cands = json.loads(result.output)["candidates"]
  assert any(c["name"] == "takk" and "stopword" in c["reasons"] for c in cands)
