"""Agent-memory adapter: the parsing that turns two agents' private memory
stores into repo-attributed items. Pure functions plus one end-to-end extract
over a synthetic store — the real stores are private."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from yaams.ingest.agent_memory import (
  AgentMemoryAdapter,
  decode_project_key,
  first_cwd,
  repo_for,
  rollout_header,
  split_task_groups,
)

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def test_decode_project_key_prefers_the_longest_existing_segment(tmp_path):
  # "cognitive-ledger" is one directory, not cognitive/ledger. The encoding
  # cannot tell them apart; the filesystem can.
  (tmp_path / "code" / "cognitive-ledger").mkdir(parents=True)
  key = "-" + str(tmp_path / "code" / "cognitive-ledger").lstrip("/").replace("/", "-")
  assert decode_project_key(key) == tmp_path / "code" / "cognitive-ledger"


def test_decode_project_key_returns_none_when_the_path_is_gone():
  assert decode_project_key("-nope-does-not-exist-anywhere") is None


def test_first_cwd_handles_both_spellings_and_trailing_punctuation():
  want = Path("/Users/me/code/x")
  assert first_cwd("(cwd=/Users/me/code/x, rollout_path=/y)") == want
  assert first_cwd("ran in cwd=/Users/me/code/x; then stopped") == want
  assert first_cwd("cwd: /Users/me/code/x\ngit_branch: main") == want
  assert first_cwd("no working directory here") is None


def test_rollout_header_stops_at_the_first_blank_line():
  head = rollout_header(
    "thread_id: abc\ncwd: /Users/me/code/x\ngit_branch: main\n\n"
    "Prose that has: a colon in it\nmore: text\n"
  )
  assert head == {"thread_id": "abc", "cwd": "/Users/me/code/x", "git_branch": "main"}


def test_split_task_groups_keeps_each_groups_own_heading():
  groups = split_task_groups("# Task Group: A\nbody a\n# Task Group: B\nbody b\n")
  assert [t for t, _ in groups] == ["A", "B"]
  assert "body a" in groups[0][1] and "body b" not in groups[0][1]


def test_split_task_groups_falls_back_to_one_chunk():
  assert [t for t, _ in split_task_groups("no headings at all")] == ["codex memory"]
  assert split_task_groups("   ") == []


def test_repo_for_walks_up_to_the_repo_and_prefers_the_innermost(tmp_path):
  outer = tmp_path / "norconsult"
  inner = outer / "NOCOS-Main" / "src"
  inner.mkdir(parents=True)
  (outer / ".git").mkdir()
  (outer / "NOCOS-Main" / ".git").mkdir()
  assert repo_for(inner, ()) == "NOCOS-Main"


def test_repo_for_falls_back_to_an_encoded_path_outside_any_repo(monkeypatch, tmp_path):
  monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
  loose = tmp_path / "scratch" / "thing"
  loose.mkdir(parents=True)
  assert repo_for(loose, ()) == "-scratch-thing"


def test_repo_for_never_keys_on_the_home_directory(monkeypatch, tmp_path):
  monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
  assert repo_for(tmp_path, ()) is None


def test_extract_attributes_both_agents_and_skips_the_claude_index(tmp_path):
  repo = tmp_path / "code" / "myrepo"
  (repo / ".git").mkdir(parents=True)

  key = "-" + str(repo).lstrip("/").replace("/", "-")
  mem = tmp_path / "projects" / key / "memory"
  mem.mkdir(parents=True)
  (mem / "a-fact.md").write_text(
    "---\nname: a-fact\ndescription: bare pytest exits 127 here\n"
    "metadata:\n  type: project\n---\n\n"
    "Use .venv/bin/pytest in this repo; the bare one is not on PATH at all.\n"
  )
  # an index over the siblings, not a fact of its own
  (mem / "MEMORY.md").write_text("# Memory Index\n\n- [a fact](a-fact.md) — hook\n")

  codex = tmp_path / "codex"
  (codex / "rollout_summaries").mkdir(parents=True)
  (codex / "rollout_summaries" / "s1.md").write_text(
    f"thread_id: t-1\nupdated_at: 2026-08-21T12:11:41+00:00\ncwd: {repo}\n"
    "git_branch: main\n\nThe session reworked the ingest adapter and its tests.\n"
  )
  (codex / "MEMORY.md").write_text(
    f"# Task Group: something\napplies_to: cwd={repo}\n\n"
    "Long enough body text to clear the minimum-content floor for ingestion.\n"
  )

  items = list(
    AgentMemoryAdapter(
      claude_projects=tmp_path / "projects", codex_memories=codex
    ).extract(EPOCH)
  )

  by_id = {i.source_id: i for i in items}
  assert f"claude/{key}/MEMORY.md" not in by_id, "the index must not be ingested"
  assert {i.raw_metadata["repo"] for i in items} == {"myrepo"}
  assert {i.source for i in items} == {"agent_memory"}
  assert {i.raw_metadata["agent"] for i in items} == {"claude", "codex"}

  claude = by_id[f"claude/{key}/a-fact.md"]
  assert claude.subject == "bare pytest exits 127 here"
  assert claude.raw_metadata["memory_type"] == "project"

  rollout = by_id["codex/rollout/s1.md"]
  assert rollout.thread_id == "t-1"
  assert rollout.timestamp_inferred is False, "the header carries a real date"


def test_extract_is_idempotent(tmp_path):
  key = "-" + str(tmp_path).lstrip("/").replace("/", "-")
  mem = tmp_path / "projects" / key / "memory"
  mem.mkdir(parents=True)
  (mem / "f.md").write_text("---\nname: f\n---\n\n" + "a durable fact worth keeping. " * 3)
  adapter = AgentMemoryAdapter(claude_projects=tmp_path / "projects")
  first = [i.id for i in adapter.extract(EPOCH)]
  second = [i.id for i in adapter.extract(EPOCH)]
  assert first == second and first, "same source must hash to the same ids"


def test_extract_respects_the_since_cutoff(tmp_path):
  key = "-" + str(tmp_path).lstrip("/").replace("/", "-")
  mem = tmp_path / "projects" / key / "memory"
  mem.mkdir(parents=True)
  old = mem / "old.md"
  old.write_text("---\nname: old\n---\n\n" + "an older durable fact worth keeping. " * 3)
  import os
  stamp = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
  os.utime(old, (stamp, stamp))

  adapter = AgentMemoryAdapter(claude_projects=tmp_path / "projects")
  assert [i.source_id for i in adapter.extract(EPOCH)] == [f"claude/{key}/old.md"]
  assert list(adapter.extract(datetime(2025, 1, 1, tzinfo=UTC))) == []


def test_timestamps_are_utc_aware(tmp_path):
  codex = tmp_path / "codex"
  (codex / "rollout_summaries").mkdir(parents=True)
  # a non-UTC offset must be normalized, not carried through
  (codex / "rollout_summaries" / "s.md").write_text(
    "thread_id: t\nupdated_at: 2026-08-21T14:11:41+02:00\ncwd: /tmp\n\n"
    "A body long enough to clear the minimum-content floor for ingestion.\n"
  )
  item = next(iter(AgentMemoryAdapter(codex_memories=codex).extract(EPOCH)))
  assert item.timestamp.tzinfo is not None
  assert item.timestamp.utcoffset().total_seconds() == 0
  assert item.timestamp.hour == 12, "14:11+02:00 is 12:11 UTC"
