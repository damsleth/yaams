"""YAAMS⇄cogled rejection-feedback contract (v1), YAAMS-read side.

`yaams promote generate` must suppress candidates the user already rejected
during cogled inbox triage, by reading cogled's
`<ledger_notes_dir>/08_indices/rejected_candidates.jsonl`. The line schema and
match precedence are pinned in cognitive-ledger/docs/yaams-cogled-interface.md
section 3.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from yaams.ingest.base import Item, hash_id
from yaams.promote.candidates import (
  PromoteConfig,
  _candidate_id,
  generate_candidates,
)
from yaams.schema import init_schema
from yaams.store import store_items
from yaams.synthesize.llm import LLMResponse

# --- fixtures / seeding ---------------------------------------------------


def _open_db() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  init_schema(conn, embedding_dim=4, use_vec=False)
  return conn


def _add_entity(conn, name: str, etype: str = "org") -> int:
  conn.execute(
    "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
    (name, etype),
  )
  conn.commit()
  return conn.execute(
    "SELECT id FROM entities WHERE canonical_name = ?", (name,)
  ).fetchone()["id"]


def _add_item(conn, key: str, entity_id: int) -> str:
  item = Item(
    id=hash_id("imessage", f"t:{key}"),
    source="imessage",
    source_id=f"t:{key}",
    timestamp=datetime.now(UTC),
    sender="a@test",
    recipients=[],
    content=f"content about the entity {key}",
    subject="",
    thread_id="t",
  )
  store_items(conn, [item], [b"\x00" * 16], [[]])
  # `_fetch_dict_entities` only considers dictionary-sourced links.
  conn.execute(
    "INSERT INTO item_entities (item_id, entity_id, source) VALUES (?, ?, ?)",
    (item.id, entity_id, "dictionary"),
  )
  conn.commit()
  return item.id


def _seed_cluster(conn, entity_name: str, n: int = 3) -> tuple[str, list[str]]:
  """One entity with `n` linked items; returns (entity_name, item_ids).

  Items are fetched by `_fetch_cluster` ordered by timestamp DESC, but the
  candidate id is order-independent only insofar as both this seed and the
  real code derive ids from the same cluster query, so we read them back the
  same way the code does."""
  eid = _add_entity(conn, entity_name)
  for i in range(n):
    _add_item(conn, f"{entity_name}-{i}", eid)
  # Mirror _fetch_cluster ordering to compute the prospective candidate id.
  rows = conn.execute(
    """
    SELECT i.id FROM item_entities ie
    JOIN items i ON i.id = ie.item_id
    WHERE ie.entity_id = ? AND i.source NOT IN ('tier2_ledger')
    ORDER BY i.timestamp DESC
    """,
    (eid,),
  ).fetchall()
  return entity_name, [r["id"] for r in rows]


_DRAFT_YAML = """\
type: fact
title: {title}
statement: A clear statement about {entity}.
tags:
  - t1
body: |
  ## Statement
  A clear statement about {entity}.
"""


class _FakeAdapter:
  backend_name = "fake"
  model_name = "fake-model"

  def __init__(self, title: str = "A drafted title"):
    self._title = title

  def complete(self, prompt, *, max_tokens=1024, temperature=0.0):
    # Pull entity out of the prompt header for a plausible draft.
    entity = "entity"
    for line in prompt.splitlines():
      if 'about "' in line:
        entity = line.split('about "', 1)[1].split('"', 1)[0]
        break
    return LLMResponse(
      text=_DRAFT_YAML.format(title=self._title, entity=entity),
      backend=self.backend_name,
      model=self.model_name,
    )


class _ExplodingAdapter:
  backend_name = "boom"
  model_name = None

  def complete(self, prompt, *, max_tokens=1024, temperature=0.0):
    raise AssertionError("_draft must not be called for a pre-draft rejection")


def _write_log(tmp_path, lines: list) -> "object":
  path = tmp_path / "rejected_candidates.jsonl"
  path.write_text(
    "".join(
      (line if isinstance(line, str) else json.dumps(line)) + "\n"
      for line in lines
    ),
    encoding="utf-8",
  )
  return path


def _cfg(tmp_path, log_path=None) -> PromoteConfig:
  return PromoteConfig(min_cluster_items=3, rejected_log_path=log_path)


# --- tests ----------------------------------------------------------------


def test_candidate_id_match_suppresses_pre_draft(tmp_path):
  # (a) exact candidate-id match suppresses before the LLM is ever called.
  conn = _open_db()
  entity, item_ids = _seed_cluster(conn, "AFOR")
  cid = _candidate_id(entity, item_ids)
  log = _write_log(tmp_path, [{
    "contract_version": 1,
    "rejected_at": "2026-06-08T09:12:00Z",
    "yaams_candidate_id": cid,
    "yaams_entity": entity,
    "yaams_source_item_ids": item_ids,
    "title": "whatever",
    "filename": "x.md",
    "reason": "discarded",
  }])
  msgs: list[str] = []
  # _ExplodingAdapter raises if _draft runs → proves the skip is pre-draft.
  out = generate_candidates(
    conn, _ExplodingAdapter(), _cfg(tmp_path, log), on_progress=msgs.append
  )
  assert out == []
  assert any("skipped (previously rejected)" in m for m in msgs)


def test_item_id_overlap_suppresses_pre_draft(tmp_path):
  # (b) overlapping source item id suppresses, even with a different/empty
  # candidate id — also pre-draft.
  conn = _open_db()
  entity, item_ids = _seed_cluster(conn, "Beta")
  log = _write_log(tmp_path, [{
    "contract_version": 1,
    "rejected_at": "2026-06-08T09:12:00Z",
    "yaams_candidate_id": "deadbeefdeadbeef",  # does NOT match
    "yaams_entity": entity,
    "yaams_source_item_ids": [item_ids[0]],  # one overlapping id
    "title": "whatever",
    "filename": "x.md",
    "reason": "merged",
  }])
  msgs: list[str] = []
  out = generate_candidates(
    conn, _ExplodingAdapter(), _cfg(tmp_path, log), on_progress=msgs.append
  )
  assert out == []
  assert any("skipped (previously rejected)" in m for m in msgs)


def test_entity_title_fallback_suppresses_pre_v1(tmp_path):
  # (c) a pre-v1 rejection lacking id + item-ids degrades to entity+title.
  conn = _open_db()
  entity, _ = _seed_cluster(conn, "Gamma")
  log = _write_log(tmp_path, [{
    # pre-v1 line: no candidate id, no source item ids
    "rejected_at": "2026-05-01T00:00:00Z",
    "yaams_candidate_id": "",
    "yaams_entity": entity,
    "yaams_source_item_ids": [],
    "title": "drafted title",  # substring of the draft title below
    "reason": "discarded",
  }])
  msgs: list[str] = []
  out = generate_candidates(
    conn,
    _FakeAdapter(title="The drafted title is here"),
    _cfg(tmp_path, log),
    on_progress=msgs.append,
  )
  assert out == []
  assert any("skipped (previously rejected)" in m for m in msgs)


def test_entity_title_fallback_requires_entity_match(tmp_path):
  # Same title text but a different entity must NOT suppress.
  conn = _open_db()
  entity, _ = _seed_cluster(conn, "Delta")
  log = _write_log(tmp_path, [{
    "yaams_candidate_id": "",
    "yaams_entity": "SomeoneElse",
    "yaams_source_item_ids": [],
    "title": "drafted title",
    "reason": "discarded",
  }])
  out = generate_candidates(
    conn, _FakeAdapter(title="The drafted title is here"), _cfg(tmp_path, log)
  )
  assert len(out) == 1
  assert out[0].entity == entity


def test_missing_log_suppresses_nothing(tmp_path):
  # (d) missing file → no suppression, no exception (degrade open).
  conn = _open_db()
  _seed_cluster(conn, "Epsilon")
  missing = tmp_path / "does_not_exist.jsonl"
  out = generate_candidates(conn, _FakeAdapter(), _cfg(tmp_path, missing))
  assert len(out) == 1


def test_empty_log_suppresses_nothing(tmp_path):
  # (d) empty file → no suppression.
  conn = _open_db()
  _seed_cluster(conn, "Zeta")
  log = tmp_path / "rejected_candidates.jsonl"
  log.write_text("", encoding="utf-8")
  out = generate_candidates(conn, _FakeAdapter(), _cfg(tmp_path, log))
  assert len(out) == 1


def test_none_log_suppresses_nothing(tmp_path):
  # rejected_log_path None (cogled not installed) → degrade open.
  conn = _open_db()
  _seed_cluster(conn, "Eta")
  out = generate_candidates(conn, _FakeAdapter(), _cfg(tmp_path, None))
  assert len(out) == 1


def test_malformed_line_is_ignored(tmp_path):
  # (e) a malformed line is skipped, and valid lines around it still apply.
  conn = _open_db()
  entity, item_ids = _seed_cluster(conn, "Theta")
  cid = _candidate_id(entity, item_ids)
  log = _write_log(tmp_path, [
    "this is not json {{{",          # malformed → skipped
    "[1,2,3]",                        # valid json but not a dict → skipped
    {"yaams_candidate_id": cid},      # valid, matches → suppress
  ])
  msgs: list[str] = []
  out = generate_candidates(
    conn, _ExplodingAdapter(), _cfg(tmp_path, log), on_progress=msgs.append
  )
  assert out == []
  assert any("skipped (previously rejected)" in m for m in msgs)
