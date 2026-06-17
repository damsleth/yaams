"""Tests for `yaams promote commit` — non-interactive ledger write verb."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yaams.ingest.base import Item, hash_id
from yaams.promote.candidates import (
  PromotionCandidate,
  fetch_pending,
  store_candidates,
)
from yaams.promote.review import write_candidate_to_ledger
from yaams.schema import init_schema
from yaams.store import store_items

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _add_entity(conn: sqlite3.Connection, name: str, etype: str = "org") -> int:
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
    (name, etype),
  )
  conn.commit()
  return conn.execute(
    "SELECT id FROM entities WHERE canonical_name = ?", (name,)
  ).fetchone()["id"]


def _add_item(conn: sqlite3.Connection, key: str, entity_name: str) -> str:
  item = Item(
    id=hash_id("imessage", f"t:{key}"),
    source="imessage",
    source_id=f"t:{key}",
    timestamp=datetime.now(UTC),
    sender="a@test",
    recipients=[],
    content=f"Content about {key}",
    subject=None,
  )
  # EntityTag = (canonical_name, entity_type, confidence, source)
  tag = (entity_name, "org", 1.0, "dictionary")
  store_items(conn, [item], [b"\x00" * 16], [[tag]])
  return item.id


def _make_candidate(
  entity: str = "TestEntity",
  title: str = "A title",
  item_ids: list[str] | None = None,
  signal_score: float | None = None,
) -> PromotionCandidate:
  """Build a minimal PromotionCandidate. Uses item_ids as source_item_ids."""
  from yaams.promote.candidates import _candidate_id

  iids = item_ids or []
  cid = _candidate_id(entity, iids)
  c = PromotionCandidate(
    id=cid,
    entity=entity,
    draft_type="fact",
    draft_title=title,
    draft_statement=f"A clear statement about {entity}.",
    draft_body=f"## Statement\nA clear statement about {entity}.",
    draft_tags=["test"],
    source_item_ids=iids,
  )
  return c


def _seed_one(conn: sqlite3.Connection, entity: str = "Alpha") -> PromotionCandidate:
  _add_entity(conn, entity)
  iids = [_add_item(conn, f"{entity}-{i}", entity) for i in range(3)]
  c = _make_candidate(entity=entity, title=f"{entity} title", item_ids=iids)
  store_candidates(conn, [c])
  return c


# ---------------------------------------------------------------------------
# Tests: write_candidate_to_ledger (shared write function)
# ---------------------------------------------------------------------------


class TestWriteCandidateToLedger:
  def test_writes_file_and_marks_accepted(self, tmp_path: Path) -> None:
    conn = _open_db()
    c = _seed_one(conn)
    row = fetch_pending(conn, "pending")[0]

    result = write_candidate_to_ledger(conn, row, tmp_path)

    assert result["status"] == "written"
    assert result["candidate_id"] == c.id
    note_path = Path(result["ledger_note"])
    assert note_path.exists()
    content = note_path.read_text()
    assert c.id in content

    # DB should now be accepted
    row2 = conn.execute(
      "SELECT status, promoted_path FROM promotion_candidates WHERE id = ?",
      (c.id,),
    ).fetchone()
    assert row2["status"] == "accepted"
    assert row2["promoted_path"] == str(note_path)

  def test_idempotent_already_accepted(self, tmp_path: Path) -> None:
    """Re-committing an already-accepted candidate returns already_accepted, no new file."""
    conn = _open_db()
    c = _seed_one(conn)
    row = fetch_pending(conn, "pending")[0]

    r1 = write_candidate_to_ledger(conn, row, tmp_path)
    assert r1["status"] == "written"

    files_after_first = list(tmp_path.rglob("*.md"))

    # Fetch again (now status=accepted) and re-commit
    row2 = conn.execute(
      "SELECT * FROM promotion_candidates WHERE id = ?", (c.id,)
    ).fetchone()
    r2 = write_candidate_to_ledger(conn, dict(row2), tmp_path)

    assert r2["status"] == "already_accepted"
    assert r2["candidate_id"] == c.id
    # No new file written
    assert list(tmp_path.rglob("*.md")) == files_after_first


# ---------------------------------------------------------------------------
# Tests: promote commit via CLI (Click test runner)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_with_candidates(tmp_path: Path):
  """Return (db_path, cfg_path, inbox_path, candidate_ids) with 3 pending candidates seeded."""
  db_path = tmp_path / "test.db"
  file_conn = sqlite3.connect(str(db_path))
  file_conn.row_factory = sqlite3.Row
  file_conn.execute("PRAGMA foreign_keys = ON")
  init_schema(file_conn, embedding_dim=4, use_vec=False)

  candidates = []
  for name in ("Alpha", "Beta", "Gamma"):
    c = _seed_one(file_conn, name)
    candidates.append(c)

  file_conn.close()

  inbox_path = tmp_path / "inbox"
  inbox_path.mkdir()

  cfg_content = f"""
db_path: {db_path}
promote:
  inbox_path: {inbox_path}
"""
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(cfg_content)

  return db_path, cfg_path, inbox_path, [c.id for c in candidates]


def _run_commit(cfg_path: Path, extra_args: list[str]) -> tuple[int, str]:
  """Run `yaams promote commit` via click.testing.CliRunner, return (exit_code, output)."""
  from click.testing import CliRunner

  import yaams.cli  # noqa: F401 — registers all subcommands
  from yaams.cli._root import cli

  runner = CliRunner()
  result = runner.invoke(
    cli,
    ["promote", "commit", "--config", str(cfg_path)] + extra_args,
    catch_exceptions=False,
  )
  return result.exit_code, result.output


class TestPromoteCommitCLI:
  def test_no_flags_exits_with_user_error(self, tmp_path: Path) -> None:
    """No targeting flags → exit code 1 (user-fixable)."""
    db_file = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    init_schema(conn, embedding_dim=4, use_vec=False)
    conn.close()

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"db_path: {db_file}\n")

    code, out = _run_commit(cfg, [])
    assert code == 1

  def test_no_flags_json_mode_error_envelope(self, tmp_path: Path) -> None:
    db_file = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    init_schema(conn, embedding_dim=4, use_vec=False)
    conn.close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"db_path: {db_file}\n")

    code, out = _run_commit(cfg, ["--json"])
    assert code == 1
    data = json.loads(out.strip())
    assert data["ok"] is False
    assert data["tool"] == "yaams"

  def test_commit_all(self, db_with_candidates: tuple) -> None:
    db_path, cfg_path, inbox_path, cids = db_with_candidates
    code, out = _run_commit(cfg_path, ["--all"])
    assert code == 0
    notes = list(inbox_path.rglob("*.md"))
    assert len(notes) == 3

  def test_commit_by_id(self, db_with_candidates: tuple) -> None:
    db_path, cfg_path, inbox_path, cids = db_with_candidates
    target = cids[0]
    code, out = _run_commit(cfg_path, ["--candidate", target])
    assert code == 0
    notes = list(inbox_path.rglob("*.md"))
    assert len(notes) == 1

  def test_commit_unknown_id_exits_error(self, db_with_candidates: tuple) -> None:
    db_path, cfg_path, inbox_path, cids = db_with_candidates
    code, out = _run_commit(cfg_path, ["--candidate", "doesnotexist123"])
    assert code == 1

  def test_commit_json_envelope_shape(self, db_with_candidates: tuple) -> None:
    db_path, cfg_path, inbox_path, cids = db_with_candidates
    code, out = _run_commit(cfg_path, ["--all", "--json"])
    assert code == 0
    data = json.loads(out.strip())

    # Required envelope fields
    assert data["tool"] == "yaams"
    assert data["command"] == "promote commit"
    assert data["ok"] is True
    assert data["exit_code"] == 0
    assert isinstance(data["promoted"], int)
    assert isinstance(data["skipped"], int)
    assert isinstance(data["items"], list)
    assert data["promoted"] == 3
    assert data["skipped"] == 0

    for item in data["items"]:
      assert "candidate_id" in item
      assert "ledger_note" in item
      assert item["status"] in ("written", "already_accepted")

  def test_idempotent_recommit_no_dupes(self, db_with_candidates: tuple) -> None:
    db_path, cfg_path, inbox_path, cids = db_with_candidates
    # First commit
    code1, out1 = _run_commit(cfg_path, ["--all", "--json"])
    assert code1 == 0
    d1 = json.loads(out1)
    assert d1["promoted"] == 3
    assert d1["skipped"] == 0

    files_after_first = sorted(p.name for p in inbox_path.rglob("*.md"))

    # Second commit — all should be skipped
    code2, out2 = _run_commit(cfg_path, ["--all", "--json"])
    assert code2 == 0
    d2 = json.loads(out2)
    assert d2["promoted"] == 0
    assert d2["skipped"] == 3

    # No new files
    files_after_second = sorted(p.name for p in inbox_path.rglob("*.md"))
    assert files_after_first == files_after_second

  def test_min_score_threshold(self, db_with_candidates: tuple) -> None:
    """--min-score with a high threshold commits nothing (no signal_score on rows)."""
    db_path, cfg_path, inbox_path, cids = db_with_candidates
    code, out = _run_commit(cfg_path, ["--min-score", "0.9", "--json"])
    assert code == 0
    data = json.loads(out)
    # All pending candidates have signal_score=None (defaults to 0), so none pass 0.9
    assert data["promoted"] == 0

  def test_min_score_zero_commits_all(self, db_with_candidates: tuple) -> None:
    """--min-score 0.0 commits all (0.0 >= 0.0 is true for every candidate)."""
    db_path, cfg_path, inbox_path, cids = db_with_candidates
    code, out = _run_commit(cfg_path, ["--min-score", "0.0", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["promoted"] == 3
