from __future__ import annotations

import json
from pathlib import Path

from yaams.config import load_config
from yaams.entities_store import (
  dedupe_dictionary,
  load_dictionary,
  save_dictionary,
  store_path,
)


def _cfg(tmp_path: Path, *, dictionary_path: str | None = None) -> dict:
  cfg: dict = {"db_path": str(tmp_path / "data.db")}
  if dictionary_path is not None:
    cfg["entities"] = {"dictionary_path": dictionary_path}
  return cfg


# --- store_path resolution ---------------------------------------------------

def test_store_path_defaults_next_to_db(tmp_path):
  assert store_path(_cfg(tmp_path)) == tmp_path / "entities.json"


def test_store_path_relative_resolves_next_to_db(tmp_path):
  assert store_path(_cfg(tmp_path, dictionary_path="ents.json")) == tmp_path / "ents.json"


def test_store_path_absolute_is_respected(tmp_path):
  abs_path = tmp_path / "elsewhere" / "e.json"
  assert store_path(_cfg(tmp_path, dictionary_path=str(abs_path))) == abs_path


# --- save / load round-trip --------------------------------------------------

def test_save_then_load_round_trips(tmp_path):
  cfg = _cfg(tmp_path)
  entries = [
    {"canonical": "Kim", "type": "person", "aliases": ["CJ"]},
    {"canonical": "Crayon", "type": "org"},
  ]
  path = save_dictionary(cfg, entries)
  assert path == tmp_path / "entities.json"
  # On disk it is a clean JSON array (no YAML, no wrapping object).
  assert json.loads(path.read_text()) == entries
  assert load_dictionary(cfg) == entries


def test_load_missing_store_is_empty(tmp_path):
  assert load_dictionary(_cfg(tmp_path)) == []


def test_load_accepts_object_wrapper(tmp_path):
  cfg = _cfg(tmp_path)
  (tmp_path / "entities.json").write_text(
    json.dumps({"entities": [{"canonical": "Kim", "type": "person"}]})
  )
  assert load_dictionary(cfg) == [{"canonical": "Kim", "type": "person"}]


def test_load_drops_entries_without_canonical(tmp_path):
  cfg = _cfg(tmp_path)
  (tmp_path / "entities.json").write_text(
    json.dumps([{"canonical": "Kim"}, {"type": "org"}, {"canonical": "  "}, "junk"])
  )
  assert load_dictionary(cfg) == [{"canonical": "Kim"}]


# --- dedupe ------------------------------------------------------------------

def test_dedupe_collapses_case_insensitive_canonicals_and_merges_aliases():
  entries = [
    {"canonical": "Crayon", "type": "org", "aliases": ["CR"]},
    {"canonical": "crayon", "aliases": ["Crayon AS", "CR"]},
    {"canonical": "Crayon", "aliases": ["Crayon"]},  # alias == canonical
  ]
  deduped, stats = dedupe_dictionary(entries)
  assert deduped == [{"canonical": "Crayon", "type": "org", "aliases": ["CR", "Crayon AS"]}]
  assert stats["dropped"] == 2


def test_dedupe_drops_alias_equal_to_canonical():
  deduped, stats = dedupe_dictionary(
    [{"canonical": "Kim", "type": "person", "aliases": ["Kim", "kim", "CJ"]}]
  )
  assert deduped == [{"canonical": "Kim", "type": "person", "aliases": ["CJ"]}]
  assert stats["dropped"] == 0


def test_dedupe_leaves_distinct_entries_alone():
  entries = [
    {"canonical": "Kim", "type": "person"},
    {"canonical": "Crayon", "type": "org"},
  ]
  deduped, stats = dedupe_dictionary(entries)
  assert deduped == entries
  assert stats == {"dropped": 0, "aliases_merged": 0}


# --- load_config integration -------------------------------------------------

def _write_config(tmp_path: Path, body: str) -> Path:
  p = tmp_path / "config.yaml"
  p.write_text(body)
  return p


def test_load_config_reads_json_store_into_dictionary(tmp_path):
  db = tmp_path / "data.db"
  (tmp_path / "entities.json").write_text(
    json.dumps([{"canonical": "Kim", "type": "person", "aliases": ["CJ"]}])
  )
  cfg_path = _write_config(
    tmp_path,
    f"db_path: {db}\nentities:\n  spacy_model: xx_ent_wiki_sm\n",
  )
  cfg = load_config(cfg_path)
  assert cfg["entities"]["dictionary"] == [
    {"canonical": "Kim", "type": "person", "aliases": ["CJ"]}
  ]
  # config-only knobs are preserved
  assert cfg["entities"]["spacy_model"] == "xx_ent_wiki_sm"


def test_json_store_overrides_inline_dictionary(tmp_path):
  db = tmp_path / "data.db"
  (tmp_path / "entities.json").write_text(json.dumps([{"canonical": "FromJson"}]))
  cfg_path = _write_config(
    tmp_path,
    f"db_path: {db}\nentities:\n  dictionary:\n    - canonical: FromInline\n",
  )
  cfg = load_config(cfg_path)
  assert [e["canonical"] for e in cfg["entities"]["dictionary"]] == ["FromJson"]


def test_inline_dictionary_kept_when_no_json_store(tmp_path):
  db = tmp_path / "data.db"  # no entities.json created
  cfg_path = _write_config(
    tmp_path,
    f"db_path: {db}\nentities:\n  dictionary:\n    - canonical: FromInline\n",
  )
  cfg = load_config(cfg_path)
  assert [e["canonical"] for e in cfg["entities"]["dictionary"]] == ["FromInline"]
