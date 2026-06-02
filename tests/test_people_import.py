from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli
from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.people_import import (
  fetch_people,
  merge_into_dictionary,
  people_to_entries,
  person_to_entry,
)
from yaams.store import get_entity_tags, resolve_entity_id


# A normalized owa-people record (every subcommand returns this shape).
def _person(name: str, email: str = "", **extra) -> dict:
  base = {
    "id": email or name,
    "displayName": name,
    "email": email,
    "jobTitle": "",
    "department": "",
    "companyName": "",
    "officeLocation": "",
    "mobilePhone": "",
    "businessPhones": [],
    "source": "directory",
  }
  base.update(extra)
  return base


# ---------------------------------------------------------------------------
# Pure mapping.
# ---------------------------------------------------------------------------


def test_person_to_entry_maps_name_and_email():
  entry = person_to_entry(_person("Vibeke Hansen", "vibeke@une.no"))
  assert entry == {"canonical": "Vibeke Hansen", "type": "person", "aliases": ["vibeke@une.no"]}


def test_person_to_entry_skips_unusable_names():
  assert person_to_entry(_person("", "x@y.no")) is None
  assert person_to_entry(_person("unknown")) is None
  # bare email as display name -> not a real name
  assert person_to_entry(_person("noreply@corp.com", "noreply@corp.com")) is None


def test_person_to_entry_omits_email_alias_equal_to_name():
  entry = person_to_entry(_person("kim@x.no", "kim@x.no"))
  # display name is email-shaped -> skipped entirely
  assert entry is None
  entry = person_to_entry(_person("Kim", "kim"))
  assert entry is not None
  assert "aliases" not in entry  # alias equal to canonical (ci) is dropped


def test_people_to_entries_folds_emails_of_same_person():
  people = [
    _person("Carl Joakim Damsleth", "carl@crayon.no"),
    _person("Carl Joakim Damsleth", "kim@damsleth.no"),
    _person("Carl Joakim Damsleth", "carl@crayon.no"),  # dup email
  ]
  entries = people_to_entries(people)
  assert len(entries) == 1
  assert entries[0]["canonical"] == "Carl Joakim Damsleth"
  assert entries[0]["aliases"] == ["carl@crayon.no", "kim@damsleth.no"]


def test_merge_into_dictionary_adds_and_updates():
  dictionary = [{"canonical": "Crayon", "type": "org"},
                {"canonical": "Vibeke Hansen", "type": "person", "aliases": ["vibeke@une.no"]}]
  new_entries = [
    {"canonical": "Ole Nordmann", "type": "person", "aliases": ["ole@corp.no"]},   # add
    {"canonical": "vibeke hansen", "type": "person", "aliases": ["vh@une.no"]},     # update (ci)
    {"canonical": "Vibeke Hansen", "type": "person", "aliases": ["vibeke@une.no"]}, # no-op (dup)
  ]
  merged, stats = merge_into_dictionary(dictionary, new_entries)
  assert stats == {"added": 1, "updated": 1, "aliases_added": 1}
  by_name = {e["canonical"]: e for e in merged}
  assert by_name["Ole Nordmann"]["aliases"] == ["ole@corp.no"]
  # case-insensitive match updated the existing entry, keeping its canonical
  assert by_name["Vibeke Hansen"]["aliases"] == ["vibeke@une.no", "vh@une.no"]
  # original dictionary is not mutated
  assert dictionary[1]["aliases"] == ["vibeke@une.no"]


def test_merge_into_dictionary_folds_shared_email_into_existing():
  # A curated entry and an incoming directory record share an email but have
  # different display names. The incoming record must fold into the existing
  # entry, not spawn a second entity that claims the same alias (which the
  # last-write-wins tagger index would silently misroute).
  dictionary = [{"canonical": "Carl Joakim Damsleth", "type": "person",
                 "aliases": ["kim@example.no"]}]
  new_entries = people_to_entries([_person("Carl Damsleth", "kim@example.no")])
  merged, stats = merge_into_dictionary(dictionary, new_entries)

  assert len(merged) == 1, merged
  assert stats == {"added": 0, "updated": 1, "aliases_added": 1}
  entry = merged[0]
  assert entry["canonical"] == "Carl Joakim Damsleth"
  # the new name variant became an alias; the email was not duplicated
  assert entry["aliases"] == ["kim@example.no", "Carl Damsleth"]
  # no two entries claim the same email
  assert sum("kim@example.no" in (e.get("aliases") or []) for e in merged) == 1


def test_merge_into_dictionary_leaves_alias_owned_by_other_entry():
  # An incoming person's email already belongs to a *different* curated entry.
  # We must not move that alias (it would create a cross-entry collision).
  dictionary = [
    {"canonical": "Support Desk", "type": "org", "aliases": ["shared@corp.no"]},
    {"canonical": "Jane Doe", "type": "person", "aliases": ["jane@corp.no"]},
  ]
  # Jane also appears under the shared mailbox address.
  new_entries = [{"canonical": "Jane Doe", "type": "person",
                  "aliases": ["shared@corp.no"]}]
  merged, stats = merge_into_dictionary(dictionary, new_entries)
  by_name = {e["canonical"]: e for e in merged}
  # shared@corp.no stays with Support Desk; Jane does not absorb it.
  assert by_name["Support Desk"]["aliases"] == ["shared@corp.no"]
  assert "shared@corp.no" not in by_name["Jane Doe"]["aliases"]
  assert stats["updated"] == 0


# ---------------------------------------------------------------------------
# Fetch (subprocess via injected runner).
# ---------------------------------------------------------------------------


def test_fetch_people_parses_me_dict_and_records_403():
  def runner(args: list[str]) -> tuple[int, str, str]:
    if "me" in args:
      return 0, json.dumps(_person("Me", "me@x.no", source="directory")), ""
    if "contacts" in args:
      return 12, "", "ERROR: access denied (403)"
    return 0, "[]", ""

  people, warnings = fetch_people(include_me=True, include_contacts=True, runner=runner)
  assert [p["displayName"] for p in people] == ["Me"]
  assert any("contacts" in w and "403" in w for w in warnings)


def test_fetch_people_runs_directory_queries():
  seen: list[list[str]] = []

  def runner(args: list[str]) -> tuple[int, str, str]:
    seen.append(args)
    if "directory" in args:
      return 0, json.dumps([_person("Dir Person", "dp@corp.no")]), ""
    return 0, "[]", ""

  people, warnings = fetch_people(
    include_me=False, include_contacts=False, queries=("norconsult",), runner=runner
  )
  assert warnings == []
  assert [p["displayName"] for p in people] == ["Dir Person"]
  assert any("directory" in a and "norconsult" in a for a in seen)


# ---------------------------------------------------------------------------
# CLI (fetch_people monkeypatched — no network).
# ---------------------------------------------------------------------------

_CONFIG = """
db_path: {db_path}

ingest:
  since: '2025-01-01T00:00:00Z'

embed:
  model: dummy
  dimension: 4

entities:
  dictionary:
    - canonical: Crayon
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


def _fake_fetch(people, warnings=None):
  def _fetch(**kwargs):
    return list(people), list(warnings or [])
  return _fetch


def test_import_people_seeds_config_and_db(tmp_path: Path, monkeypatch):
  cfg = _config(tmp_path)
  monkeypatch.setattr(
    "yaams.cli.entities.fetch_people",
    _fake_fetch([
      _person("Vibeke Hansen", "vibeke@une.no"),
      _person("Ole Nordmann", "ole@corp.no"),
    ]),
  )
  result = CliRunner().invoke(
    cli, ["entities", "import-people", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 0, result.output
  stats = json.loads(result.output)["stats"]
  assert stats["fetched"] == 2
  assert stats["added"] == 2

  # Durable in config dictionary...
  dictionary = load_config(str(cfg))["entities"]["dictionary"]
  names = {e["canonical"] for e in dictionary}
  assert {"Crayon", "Vibeke Hansen", "Ole Nordmann"} <= names

  # ...and seeded into the DB so tagging/associations can use them.
  conn = open_db(get_db_path(load_config(str(cfg))), readonly=True)
  try:
    assert resolve_entity_id(conn, "Vibeke Hansen") is not None
    assert resolve_entity_id(conn, "Ole Nordmann") is not None
  finally:
    conn.close()


def test_import_people_dry_run_writes_nothing(tmp_path: Path, monkeypatch):
  cfg = _config(tmp_path)
  before = load_config(str(cfg))["entities"]["dictionary"]
  monkeypatch.setattr(
    "yaams.cli.entities.fetch_people",
    _fake_fetch([_person("Vibeke Hansen", "vibeke@une.no")]),
  )
  result = CliRunner().invoke(
    cli, ["entities", "import-people", "--config", str(cfg), "--dry-run", "--json"]
  )
  assert result.exit_code == 0, result.output
  stats = json.loads(result.output)["stats"]
  assert stats["dry_run"] is True and stats["added"] == 1

  # Config unchanged, DB has no such entity.
  assert load_config(str(cfg))["entities"]["dictionary"] == before
  conn = open_db(get_db_path(load_config(str(cfg))), readonly=True)
  try:
    assert resolve_entity_id(conn, "Vibeke Hansen") is None
  finally:
    conn.close()


def test_import_people_applies_tags(tmp_path: Path, monkeypatch):
  cfg = _config(tmp_path)
  monkeypatch.setattr(
    "yaams.cli.entities.fetch_people",
    _fake_fetch([_person("Vibeke Hansen", "vibeke@une.no")]),
  )
  result = CliRunner().invoke(
    cli, ["entities", "import-people", "--config", str(cfg), "--tag", "une", "--json"]
  )
  assert result.exit_code == 0, result.output
  conn = open_db(get_db_path(load_config(str(cfg))), readonly=True)
  try:
    eid = resolve_entity_id(conn, "Vibeke Hansen")
    assert "une" in get_entity_tags(conn, eid)
  finally:
    conn.close()


def test_import_people_partial_failure_is_a_warning(tmp_path: Path, monkeypatch):
  cfg = _config(tmp_path)
  monkeypatch.setattr(
    "yaams.cli.entities.fetch_people",
    _fake_fetch([_person("Me", "me@x.no")], warnings=["contacts: access denied (403)"]),
  )
  result = CliRunner().invoke(
    cli, ["entities", "import-people", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 0, result.output
  env = json.loads(result.output)
  assert env["ok"] is True
  assert any("contacts" in w for w in env["warnings"])


def test_import_people_all_failed_is_error(tmp_path: Path, monkeypatch):
  cfg = _config(tmp_path)
  monkeypatch.setattr(
    "yaams.cli.entities.fetch_people",
    _fake_fetch([], warnings=["me: access denied (403)", "contacts: access denied (403)"]),
  )
  result = CliRunner().invoke(
    cli, ["entities", "import-people", "--config", str(cfg), "--json"]
  )
  assert result.exit_code == 1, result.output
  env = json.loads(result.output)
  assert env["ok"] is False
  assert env["error"]["code"] == "all_sources_failed"
