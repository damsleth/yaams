"""Trust-verdict attachment over retrieval results + CLI surfacing."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli
from yaams.ingest.base import Item, hash_id
from yaams.retrieve import HybridResult, attach_trust_verdicts, trust_to_dict
from yaams.schema import init_schema
from yaams.signals import log_feedback, log_query, new_query_id
from yaams.store import store_items
from yaams.trust import TrustVerdict


def _db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _store(conn, source: str, content: str, *, ts=None) -> str:
  ts = ts or datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
  item = Item(
    id=hash_id(source, content),
    source=source,
    source_id=content,
    timestamp=ts,
    sender="a@test",
    recipients=[],
    content=content,
    subject="",
  )
  store_items(conn, [item], [b"\x00" * 16], [[]])
  return item.id


def _result(item_id: str, source: str, score: float) -> HybridResult:
  return HybridResult(
    id=item_id,
    kind="item",
    source=source,
    timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    sender="a@test",
    subject="",
    content="",
    thread_id=None,
    score=score,
  )


def test_attach_sets_verdict_on_each_result():
  conn = _db()
  iid = _store(conn, "email", "hello world")
  results = [_result(iid, "email", 0.9)]
  attach_trust_verdicts(results, conn)
  assert isinstance(results[0].trust, TrustVerdict)
  assert results[0].trust.level in {"high", "medium", "low"}


def test_attach_never_reorders():
  conn = _db()
  ids = [_store(conn, "email", f"msg {i}") for i in range(5)]
  results = [_result(i, "email", 1.0 - n * 0.1) for n, i in enumerate(ids)]
  before = [r.id for r in results]
  attach_trust_verdicts(results, conn, provenance_weighting_enabled=True)
  assert [r.id for r in results] == before


def test_show_trust_verdict_false_is_noop():
  conn = _db()
  iid = _store(conn, "email", "hello")
  results = [_result(iid, "email", 0.9)]
  attach_trust_verdicts(results, conn, show_trust_verdict=False)
  assert results[0].trust is None


def test_provenance_weighting_distinguishes_sources():
  conn = _db()
  email_id = _store(conn, "email", "authored note")
  chat_id = _store(conn, "imessage", "chat note")
  results = [_result(email_id, "email", 0.9), _result(chat_id, "imessage", 0.9)]
  attach_trust_verdicts(results, conn, provenance_weighting_enabled=True)
  by_id = {r.id: r.trust for r in results}
  # email (authored, 0.92) clears the high band; imessage (conversational,
  # 0.82) lands in medium.
  assert by_id[email_id].level == "high"
  assert by_id[chat_id].level == "medium"


def test_feedback_counts_drive_verdict():
  conn = _db()
  iid = _store(conn, "imessage", "needs affirming")
  qid = new_query_id()
  log_query(
    conn,
    query_id=qid,
    text="q",
    top_k=10,
    source_filter=None,
    since=None,
    until=None,
    results=[],
  )
  log_feedback(conn, query_id=qid, kind="correction", result_id=iid)
  results = [_result(iid, "imessage", 0.9)]
  attach_trust_verdicts(results, conn, provenance_weighting_enabled=True)
  assert results[0].trust.level == "low"
  assert "contradicted" in results[0].trust.reason


def test_trust_to_dict_shapes():
  assert trust_to_dict(None) is None
  d = trust_to_dict(TrustVerdict("high", "why", 0.876543))
  assert d == {"level": "high", "reason": "why", "score": 0.8765}


# --- CLI surfacing ----------------------------------------------------------

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


def test_query_json_includes_trust(tmp_path: Path):
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG.format(db_path=db))
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])

  from yaams.db import open_db

  conn = open_db(str(db))
  _store(conn, "email", "alpha beta gamma")
  conn.commit()
  conn.close()

  result = CliRunner().invoke(
    cli,
    ["query", "--config", str(cfg), "--no-vector", "--no-parse", "--json", "alpha"],
  )
  assert result.exit_code == 0, result.output
  payload = json.loads(result.output.strip())
  assert payload["results"], "expected an FTS hit"
  assert "trust" in payload["results"][0]
  assert payload["results"][0]["trust"]["level"] in {"high", "medium", "low"}
