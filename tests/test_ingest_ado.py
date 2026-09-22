from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from yaams.ingest.ado import AdoAdapter, strip_html, wiki_page_title, wiki_page_to_item
from yaams.ingest.base import hash_id

FIXTURES = Path(__file__).parent / "fixtures" / "ado"


def _fake_run(calls: list[list[str]]):
  rows = json.loads((FIXTURES / "wiql_rows.json").read_text())
  full = json.loads((FIXTURES / "workitem_full.json").read_text())

  def run(self, args: list[str], timeout=None):
    calls.append(args)
    if args[:2] == ["wi", "--query"]:
      return rows if "[System.Id] > 0" in args[2] else []
    if args[0] == "wi" and args[-1] == "--full":
      return dict(full, id=int(args[1])) if args[1] == "17964" else None
    if args[0] == "wiki":
      return {"downloaded": 0, "dir": args[2], "files": []}
    raise AssertionError(args)

  return run


def test_strip_html_keeps_structure_and_entities():
  out = strip_html("<div>a &amp; b</div><ul><li>one</li><li><strike>two</strike></li></ul>")
  assert out == "a & b\none\ntwo"
  assert "<" not in out


def test_work_items_from_wiql_response(tmp_path, monkeypatch):
  calls: list[list[str]] = []
  monkeypatch.setattr(AdoAdapter, "_run", _fake_run(calls))
  adapter = AdoAdapter(profile="nc", wiki_dir=tmp_path, content_types=("workitems",), top=500)
  items = list(adapter.extract(datetime(2026, 9, 22, 3, 0, tzinfo=UTC)))

  wiql = calls[0][2]
  assert "[System.ChangedDate] >= '2026-09-21'" in wiql
  assert "T" not in wiql.split("'")[1]
  # 15661 changed before the cutoff on the same day: filtered, never fetched.
  fetched = [c[1] for c in calls if c[0] == "wi" and c[-1] == "--full"]
  assert fetched == ["17964"]

  assert len(items) == 1
  item = items[0]
  assert item.source == "ado_nc"
  assert item.source_id == "17964:18"
  assert item.id == hash_id("ado_nc", "17964:18")
  assert item.timestamp == datetime(2026, 9, 22, 6, 21, 22, 637000, tzinfo=UTC)
  assert item.subject == "Configure support role list & lookup"
  assert "<" not in item.content
  assert "Quality coordinator" in item.content
  assert "Acceptance criteria:\nList renders in the dialog." in item.content
  assert item.sender == "Placeholder Person"
  assert item.recipients == ["Placeholder Person"]
  assert item.thread_id == "17665"
  assert item.timestamp_inferred is False
  meta = item.raw_metadata
  assert meta["type"] == "Task"
  assert meta["state"] == "Active"
  assert meta["tags"] == ["Example Tag", "Another"]
  assert meta["iteration"] == "EXAMPLE\\Sprint 1"
  assert meta["comment_count"] == 2
  assert meta["url"].endswith("/_workitems/edit/17964")
  assert adapter.work_items == 1


def test_wiki_markdown_to_item(tmp_path, monkeypatch):
  calls: list[list[str]] = []
  monkeypatch.setattr(AdoAdapter, "_run", _fake_run(calls))
  mirror = tmp_path / "nc" / "EXAMPLE" / "Ops"
  mirror.mkdir(parents=True)
  page = mirror / "Documenting-BUGS-%2D-example.md"
  shutil.copy(FIXTURES / "Documenting-BUGS-%2D-example.md", page)

  adapter = AdoAdapter(profile="nc", wiki_dir=tmp_path, content_types=("wiki",))
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))
  assert calls == [["wiki", "--download", str(tmp_path / "nc")]]

  assert len(items) == 1
  item = items[0]
  assert item.source == "ado_nc"
  assert item.subject == "Documenting BUGS - example"
  assert item.source_id.startswith("/EXAMPLE/Ops/Documenting-BUGS-%2D-example:")
  digest = item.source_id.rsplit(":", 1)[1]
  assert len(digest) == 8 and int(digest, 16) >= 0
  assert item.timestamp_inferred is True
  assert item.timestamp == datetime.fromtimestamp(page.stat().st_mtime, tz=UTC)
  assert item.thread_id == "/EXAMPLE/Ops"
  assert "<h2>" not in item.content
  assert "Tagging" in item.content
  assert item.raw_metadata["kind"] == "wiki"

  again = wiki_page_to_item(page, tmp_path / "nc", "nc")
  assert again is not None and again.id == item.id


def test_wiki_page_title_decodes_mirror_names():
  assert wiki_page_title(Path("Arrangementsystem-%2D-B%C3%A6rekraftuken.md")) == (
    "Arrangementsystem - Bærekraftuken"
  )
