"""The retrieval harness refuses to score a gold set with a junk-annotated gold.

Builds a throwaway db with one gold pointing at a ``mech:short`` item and checks
that ``scripts/autoresearch_retrieval.py`` exits 1 with ``status: invalid_gold``,
and that ``--allow-junk-gold`` bypasses the guard. The scorer's metric code is
off-limits; this only pins the precondition check in front of it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from yaams.schema import init_schema

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "autoresearch_retrieval.py"


def _load_harness():
  spec = importlib.util.spec_from_file_location("autoresearch_retrieval", _SCRIPT)
  mod = importlib.util.module_from_spec(spec)
  assert spec is not None and spec.loader is not None
  spec.loader.exec_module(mod)
  return mod


def _build_db(path: Path, junk_reason: str | None) -> None:
  conn = sqlite3.connect(path)
  conn.row_factory = sqlite3.Row
  init_schema(conn, embedding_dim=4, use_vec=False)
  conn.execute(
    "INSERT INTO items (id, source, source_id, timestamp, sender, recipients, content, "
    "ingested_at, junk_reason) VALUES (?, 'imessage', 'm1', '2026-05-01T10:00:00+00:00', "
    "'me', '[]', 'ok', '2026-05-01T10:00:00+00:00', ?)",
    ("item-1", junk_reason),
  )
  conn.execute(
    "INSERT INTO queries (id, text, top_k, ts) VALUES ('q1', 'ok', 10, '2026-05-02T10:00:00+00:00')"
  )
  conn.execute(
    "INSERT INTO query_feedback (query_id, kind, result_id, ts) "
    "VALUES ('q1', 'hit', 'item-1', '2026-05-02T10:01:00+00:00')"
  )
  conn.execute(
    "INSERT INTO queries (id, text, top_k, ts) VALUES ('q2', 'other', 10, '2026-05-02T10:00:00+00:00')"
  )
  conn.execute(
    "INSERT INTO query_feedback (query_id, kind, result_id, ts) "
    "VALUES ('q2', 'hit', 'cons:abc', '2026-05-02T10:01:00+00:00')"
  )
  conn.commit()
  conn.close()


@pytest.fixture
def harness_env(tmp_path, monkeypatch):
  cfg = tmp_path / "config.yaml"
  cfg.write_text("{}\n")
  monkeypatch.setenv("YAAMS_CONFIG", str(cfg))
  monkeypatch.delenv("YAAMS_AUTORESEARCH_DB", raising=False)
  return tmp_path


def _run(monkeypatch, argv: list[str]) -> tuple[int, str]:
  mod = _load_harness()
  monkeypatch.setattr(sys, "argv", ["autoresearch_retrieval.py", *argv])
  buf = io.StringIO()
  with redirect_stdout(buf):
    rc = mod.main()
  return rc, buf.getvalue()


def test_junk_gold_helper_skips_consolidation_ids(tmp_path):
  db = tmp_path / "fixture.db"
  _build_db(db, "mech:short")
  mod = _load_harness()
  conn = sqlite3.connect(db)
  conn.row_factory = sqlite3.Row
  gold, _, _ = mod._load_gold(conn)
  bad = mod._junk_gold(conn, gold)
  conn.close()
  assert len(gold) == 2
  assert bad == [("ok", "item-1", "mech:short")]


def test_guard_trips_on_junk_gold(harness_env, monkeypatch, capsys):
  db = harness_env / "fixture.db"
  _build_db(db, "mech:short")
  rc, out = _run(
    monkeypatch, ["--db", str(db), "--no-vector", "--no-write", "--split", "all", "--json"]
  )
  assert rc == 1
  summary = json.loads(out)
  assert summary["status"] == "invalid_gold"
  assert summary["junk_gold"] == 1
  err = capsys.readouterr().err
  assert "item-1" in err and "mech:short" in err


def test_allow_junk_gold_bypasses_guard(harness_env, monkeypatch):
  db = harness_env / "fixture.db"
  _build_db(db, "mech:short")
  rc, out = _run(
    monkeypatch,
    ["--db", str(db), "--no-vector", "--no-write", "--split", "all", "--json",
     "--allow-junk-gold"],
  )
  assert rc == 0
  summary = json.loads(out)
  assert summary["status"] != "invalid_gold"
  assert summary["gold_queries"] == 2


def test_clean_gold_passes_guard(harness_env, monkeypatch):
  db = harness_env / "fixture.db"
  _build_db(db, None)
  rc, out = _run(
    monkeypatch, ["--db", str(db), "--no-vector", "--no-write", "--split", "all", "--json"]
  )
  assert rc == 0
  assert json.loads(out)["status"] != "invalid_gold"
