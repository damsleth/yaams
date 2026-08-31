"""Tests for `entities rename` and `entities unalias`.

Both exist because a routine cleanup had no supported path: the dictionary
could only gain aliases (via `add`/`merge`/`import-*`), and the only way to
change a canonical name was to create the target and merge into it. Fixing one
wrong alias meant editing the JSON store by hand and reseeding.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli

_CONFIG = """
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'

embed:
  model: dummy
  dimension: 4

entities:
  dictionary:
    - canonical: Nina
      type: person
      aliases: ['+4794324297', 'Mamma']
    - canonical: Norconsult
      type: org

synthesize:
  llm:
    backend: dummy
"""


def _config(tmp_path: Path) -> Path:
  db = tmp_path / "data.db"
  cfg = tmp_path / "config.yaml"
  cfg.write_text(_CONFIG.format(db_path=db))
  CliRunner().invoke(cli, ["init-db", "--config", str(cfg)])
  return cfg


def _run(cfg, *args):
  return CliRunner().invoke(cli, [*args, "--config", str(cfg)])


def _show(cfg, name) -> dict:
  res = _run(cfg, "entities", "show", name, "--json")
  assert res.exit_code == 0, res.output
  return json.loads(res.output)


def _aliases(cfg, name) -> list[str]:
  return _show(cfg, name)["aliases"]


def _stored_aliases(cfg: Path, name: str) -> list[str]:
  """Read the JSON dictionary store directly, bypassing the DB, to prove the
  change was persisted and not just applied to the entities table."""
  store = json.loads((cfg.parent / "entities.json").read_text())
  entries = store["entities"] if isinstance(store, dict) else store
  for entry in entries:
    if entry["canonical"].casefold() == name.casefold():
      return list(entry.get("aliases") or [])
  raise AssertionError(f"{name!r} not in the dictionary store")


def test_rename_keeps_the_old_name_as_an_alias(tmp_path: Path):
  """The corpus still says "Nina", so the old name has to keep resolving."""
  cfg = _config(tmp_path)
  res = _run(cfg, "entities", "rename", "Nina", "Nina Cathrine Damsleth")
  assert res.exit_code == 0, res.output

  # The old name no longer resolves as a canonical...
  stale = _run(cfg, "entities", "show", "Nina")
  assert stale.exit_code != 0
  assert "No entity named" in stale.output
  # ...but it survives as an alias, so historical mentions still land.
  aliases = _aliases(cfg, "Nina Cathrine Damsleth")
  assert "Nina" in aliases
  # Pre-existing aliases survive the rename.
  assert "+4794324297" in aliases and "Mamma" in aliases


def test_rename_survives_a_reload(tmp_path: Path):
  """The rename has to land in the dictionary store, not just the DB row, or
  the next reseed resurrects the old canonical."""
  cfg = _config(tmp_path)
  assert _run(cfg, "entities", "rename", "Nina", "Nina Cathrine Damsleth").exit_code == 0
  assert "Nina Cathrine Damsleth" in _run(cfg, "entities", "list").output
  # Persisted, so the next reseed does not resurrect the old canonical.
  assert "Nina" in _stored_aliases(cfg, "Nina Cathrine Damsleth")


def test_rename_preserves_tags_and_meta(tmp_path: Path):
  """Renaming in place must keep the entity row, so everything hanging off it
  follows without repointing."""
  cfg = _config(tmp_path)
  assert _run(cfg, "entities", "tag", "Nina", "family").exit_code == 0
  assert _run(cfg, "entities", "rename", "Nina", "Nina Cathrine Damsleth").exit_code == 0
  out = _run(cfg, "entities", "show", "Nina Cathrine Damsleth").output
  assert "family" in out


def test_rename_refuses_to_clobber_another_entity(tmp_path: Path):
  """Renaming onto an existing name would silently need a merge; say so
  instead of guessing which one wins."""
  cfg = _config(tmp_path)
  res = _run(cfg, "entities", "rename", "Nina", "Norconsult")
  assert res.exit_code != 0
  assert "already names a different entity" in res.output
  assert "merge" in res.output
  # Both entities are untouched.
  assert _run(cfg, "entities", "show", "Nina").exit_code == 0
  assert _run(cfg, "entities", "show", "Norconsult").exit_code == 0


def test_rename_can_drop_the_old_name_for_a_typo(tmp_path: Path):
  cfg = _config(tmp_path)
  res = _run(cfg, "entities", "rename", "Nina", "Nina Damsleth", "--drop-old-alias")
  assert res.exit_code == 0, res.output
  aliases = _aliases(cfg, "Nina Damsleth")
  assert "Nina" not in aliases
  assert "Mamma" in aliases


def test_unalias_removes_only_the_named_alias(tmp_path: Path):
  cfg = _config(tmp_path)
  res = _run(cfg, "entities", "unalias", "Nina", "Mamma")
  assert res.exit_code == 0, res.output
  aliases = _aliases(cfg, "Nina")
  assert "Mamma" not in aliases
  assert "+4794324297" in aliases
  # The entity itself is still there: unalias is not remove.
  assert _run(cfg, "entities", "show", "Nina").exit_code == 0


def test_unalias_is_case_insensitive_and_reports_misses(tmp_path: Path):
  cfg = _config(tmp_path)
  res = _run(cfg, "entities", "unalias", "Nina", "mamma", "Pappa", "--json")
  assert res.exit_code == 0, res.output
  stats = json.loads(res.output)["stats"]
  assert stats["removed"] == ["Mamma"]
  assert stats["not_found"] == ["pappa"]


def test_unalias_survives_a_reload(tmp_path: Path):
  """seed_entities rewrites the aliases column wholesale, so the removal only
  sticks if it reached the dictionary store."""
  cfg = _config(tmp_path)
  assert _run(cfg, "entities", "unalias", "Nina", "Mamma").exit_code == 0
  assert "Mamma" not in _stored_aliases(cfg, "Nina")
  assert "Mamma" not in _aliases(cfg, "Nina")
