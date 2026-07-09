from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from yaams.ingest.chats_facts import (
  ChatsFactsAdapter,
  _parse_bullets,
  facts_from_file,
)

_SUMMARY = """---
created: 2026-07-02T09:00:00Z
session_id: sess-123
tags: [ledger, config]
---

# Panorama search in yaams

## Summary

Looked into Panorama and the ledger config split.

## Insights / Facts

- **Panorama** (Norconsult) is a staffing front-end layered over Genus.
- Config split: `ledger_root` is the repo, `ledger_notes_dir` the notes dir.
- ok
- Archived digest notes need Z-suffixed UTC timestamps, not +00:00.

## Open loops

- Rotate the leaked Sparx EA license keys.
"""


def _write(path: Path, body: str) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(body, encoding="utf-8")
  return path


def test_parse_bullets_drops_short_and_keeps_order() -> None:
  bullets = _parse_bullets(
    "- first fact that is long enough to keep\n"
    "- ok\n"  # below MIN_FACT_CHARS -> dropped
    "- second fact that is also plenty long\n"
  )
  assert bullets == [
    "first fact that is long enough to keep",
    "second fact that is also plenty long",
  ]


def test_facts_from_file_extracts_insights_section_only(tmp_path: Path) -> None:
  f = _write(tmp_path / "2026-07-02-panorama.md", _SUMMARY)
  facts = facts_from_file(f, tmp_path)

  contents = [r.content for r in facts]
  # Three long Insights bullets; "ok" dropped; Open-loops bullet not extracted.
  assert len(facts) == 3
  assert contents[0].startswith("**Panorama**")
  assert all("Sparx" not in c for c in contents)

  r = facts[0]
  assert r.subject == "Panorama search in yaams"
  assert r.session_id == "sess-123"
  assert r.tags == ["ledger", "config"]
  assert r.timestamp == datetime(2026, 7, 2, 9, 0, tzinfo=UTC)
  assert not r.timestamp_inferred
  # source_id is content-hashed -> stable across re-ingest, changes if edited.
  assert r.source_id.startswith("2026-07-02-panorama.md#")


def test_source_id_stable_across_reparse(tmp_path: Path) -> None:
  f = _write(tmp_path / "x.md", _SUMMARY)
  a = [r.source_id for r in facts_from_file(f, tmp_path)]
  b = [r.source_id for r in facts_from_file(f, tmp_path)]
  assert a == b


def test_no_insights_section_yields_nothing(tmp_path: Path) -> None:
  f = _write(
    tmp_path / "plain.md",
    "---\ncreated: 2026-07-02T09:00:00Z\n---\n\n# Title\n\n## Summary\n\nBody only.\n",
  )
  assert facts_from_file(f, tmp_path) == []


def test_facts_route_to_isolated_indexes_and_default_query_excludes_them(
  tmp_path: Path,
) -> None:
  """A chats_facts item must land in the separate chats_facts_fts/_vec tables
  and NOT the shared items_fts/items_vec, so it can't perturb default retrieval.
  It is reachable only when the query targets the chats_facts source."""
  from yaams.db import open_db
  from yaams.ingest.base import Item, hash_id
  from yaams.retrieve.hybrid import HybridQueryConfig, query
  from yaams.schema import FACTS_SOURCE, init_schema
  from yaams.store import store_items

  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)

  fact = Item(
    id=hash_id(FACTS_SOURCE, "s.md#abc123"),
    source=FACTS_SOURCE,
    source_id="s.md#abc123",
    timestamp=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
    sender="me",
    recipients=[],
    content="Panorama is a staffing front-end layered over Genus.",
    subject="Panorama notes",
  )
  normal = Item(
    id=hash_id("email", "<n@x.test>"),
    source="email",
    source_id="<n@x.test>",
    timestamp=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
    sender="a@x.test",
    recipients=[],
    content="Unrelated email that never mentions the staffing tool.",
    subject="hello",
  )
  store_items(conn, [fact, normal], [[0.1, 0.2, 0.3]] * 2, [[], []])

  def count(sql: str) -> int:
    return conn.execute(sql).fetchone()[0]

  # Fact indexed only in the isolated tables.
  assert count("SELECT count(*) FROM chats_facts_fts") == 1
  assert count("SELECT count(*) FROM chats_facts_vec") == 1
  assert count(
    f"SELECT count(*) FROM items_fts WHERE item_id = '{fact.id}'"
  ) == 0
  assert count(
    f"SELECT count(*) FROM items_vec WHERE item_id = '{fact.id}'"
  ) == 0

  # Default query (FTS-only, embedding=None) can't see the fact.
  default_hits = query(conn, "Panorama staffing tool", config=HybridQueryConfig())
  assert all(r.source != FACTS_SOURCE for r in default_hits)

  # Explicit fact-tier query returns it from the isolated index.
  tier_hits = query(
    conn,
    "Panorama staffing tool",
    config=HybridQueryConfig(source_filter=[FACTS_SOURCE]),
  )
  assert any(r.source == FACTS_SOURCE for r in tier_hits)


def test_generate_fact_candidates_and_stable_ids(tmp_path: Path) -> None:
  """Sink 2: each Insights/Facts bullet becomes one entity-less fact candidate,
  with a stable id so re-running store_candidates is idempotent."""
  from yaams.promote.facts import _fact_title, generate_fact_candidates

  _write(tmp_path / "2026-07-02-panorama.md", _SUMMARY)
  cands = generate_fact_candidates(tmp_path)

  assert len(cands) == 3
  assert all(c.entity == "" for c in cands)
  assert all(c.draft_type == "fact" for c in cands)
  assert all(c.backend == "chats_facts" for c in cands)
  assert all(len(c.source_item_ids) == 1 for c in cands)
  # Bullet text preserved verbatim as the statement.
  assert any("Genus" in c.draft_statement for c in cands)

  # Re-running yields identical ids (idempotency contract for store_candidates).
  again = generate_fact_candidates(tmp_path)
  assert [c.id for c in cands] == [c.id for c in again]

  # since filter drops facts older than the cutoff (summary dated 2026-07-02).
  future = generate_fact_candidates(tmp_path, since=datetime(2026, 8, 1, tzinfo=UTC))
  assert future == []

  assert _fact_title("**Bold lead** then a clause; and more.") == "Bold lead then a clause"


def test_adapter_emits_items_with_source(tmp_path: Path) -> None:
  _write(tmp_path / "2026-07-02-panorama.md", _SUMMARY)
  adapter = ChatsFactsAdapter(chats_path=tmp_path)
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  assert len(items) == 3
  assert all(it.source == "chats_facts" for it in items)
  assert all(it.sender == "me" for it in items)
  assert all(it.raw_metadata.get("fact") is True for it in items)
  # Distinct ids per fact.
  assert len({it.id for it in items}) == 3
