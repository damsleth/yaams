from __future__ import annotations

from pathlib import Path

import pytest

from yaams.cli import sources as sources_mod
from yaams.cli.sources import (
  SourceRow,
  SubPathRow,
  _build_rows,
  _rewrite_enabled_flags,
  _yaml_append_email_source,
  _yaml_append_folder_path,
  _yaml_remove_email_source,
  _yaml_remove_folder_path,
  _yaml_set_email_entry_enabled,
  _yaml_set_folder_entry_enabled,
  _yaml_set_profile_enabled,
)

SAMPLE = """\
db_path: ~/yaams/data.db

ingest:
  since: '2025-01-01T00:00:00Z'

  imessage:
    enabled: true
    chat_db_path: ~/Library/Messages/chat.db

  email:
    enabled: true
    sources:
      - type: emlx
        path: ~/Library/Mail/V10
      - type: mbox
        path: ~/Downloads/all.mbox
    skip_newsletters: true

  folders:
    enabled: false
    paths:
      - ~/Documents/notes
      - ~/work/specs

  calendar:
    enabled: false
    profiles:
      - swon
      - dno

  teams:
    enabled: false
    profiles:
      - swon
"""


SAMPLE_NO_FOLDERS = """\
db_path: ~/yaams/data.db

ingest:
  since: '2025-01-01T00:00:00Z'

  imessage:
    enabled: true
    chat_db_path: ~/Library/Messages/chat.db
"""


def _write(tmp_path: Path, body: str = SAMPLE) -> Path:
  p = tmp_path / "config.yaml"
  p.write_text(body)
  return p


@pytest.fixture(autouse=True)
def _clear_cache():
  sources_mod._clear_profile_cache()
  yield
  sources_mod._clear_profile_cache()


def _stub_discovery(monkeypatch: pytest.MonkeyPatch, calendar=None, teams=None) -> None:
  monkeypatch.setattr(
    sources_mod, "discover_calendar_profiles",
    lambda: list(calendar or []),
  )
  monkeypatch.setattr(
    sources_mod, "discover_teams_profiles",
    lambda: list(teams or []),
  )


def test_rewrite_enabled_flags_only_changes_targeted_lines(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  changed = _rewrite_enabled_flags(cfg_path, {"imessage": False, "folders": True})
  assert changed == {"imessage": False, "folders": True}
  text = cfg_path.read_text()
  assert "imessage:\n    enabled: false" in text
  assert "folders:\n    enabled: true" in text
  assert "email:\n    enabled: true" in text


def test_append_email_source(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_append_email_source(cfg_path, "mbox", "~/Downloads/extra.mbox")
  text = cfg_path.read_text()
  assert "- type: mbox\n        path: ~/Downloads/extra.mbox" in text
  assert text.count("- type:") == 3


def test_remove_email_source_by_index(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_remove_email_source(cfg_path, 0)
  text = cfg_path.read_text()
  assert "~/Library/Mail/V10" not in text
  assert "~/Downloads/all.mbox" in text
  assert "skip_newsletters: true" in text


def test_append_folder_path(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_append_folder_path(cfg_path, "~/new/folder")
  text = cfg_path.read_text()
  assert "- ~/new/folder" in text
  assert "- ~/Documents/notes" in text
  assert "- ~/work/specs" in text


def test_remove_folder_path_by_index(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_remove_folder_path(cfg_path, 0)
  text = cfg_path.read_text()
  assert "- ~/Documents/notes" not in text
  assert "- ~/work/specs" in text


def test_append_folder_path_creates_block_when_missing(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path, SAMPLE_NO_FOLDERS)
  _yaml_append_folder_path(cfg_path, "~/Documents/notes")
  text = cfg_path.read_text()
  assert "folders:" in text
  assert "enabled: false" in text
  assert "- ~/Documents/notes" in text

  import yaml
  cfg = yaml.safe_load(text)
  assert cfg["ingest"]["folders"]["paths"] == ["~/Documents/notes"]
  assert cfg["ingest"]["folders"]["enabled"] is False


def test_append_folder_path_handles_inline_empty_list(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  folders:\n"
    "    enabled: false\n"
    "    paths: []\n"
  )
  cfg_path = _write(tmp_path, body)
  _yaml_append_folder_path(cfg_path, "~/first")
  text = cfg_path.read_text()
  assert "paths: []" not in text
  assert "- ~/first" in text


def test_set_folder_entry_disabled_converts_bare_string(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_set_folder_entry_enabled(cfg_path, 0, enabled=False)
  text = cfg_path.read_text()
  assert "- path: ~/Documents/notes\n        enabled: false" in text
  assert "- ~/work/specs" in text  # other entry untouched

  import yaml
  cfg = yaml.safe_load(text)
  entries = cfg["ingest"]["folders"]["paths"]
  assert entries[0] == {"path": "~/Documents/notes", "enabled": False}
  assert entries[1] == "~/work/specs"


def test_set_folder_entry_re_enable_flips_flag(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  folders:\n"
    "    enabled: true\n"
    "    paths:\n"
    "      - path: ~/a\n"
    "        enabled: false\n"
    "      - ~/b\n"
  )
  cfg_path = _write(tmp_path, body)
  _yaml_set_folder_entry_enabled(cfg_path, 0, enabled=True)
  text = cfg_path.read_text()
  assert "enabled: true" in text
  assert "- path: ~/a" in text


def test_set_email_entry_enabled_inserts_flag(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_set_email_entry_enabled(cfg_path, 1, enabled=False)
  text = cfg_path.read_text()
  assert "- type: mbox\n        path: ~/Downloads/all.mbox\n        enabled: false" in text
  # first entry unchanged
  assert "- type: emlx\n        path: ~/Library/Mail/V10" in text
  import yaml
  cfg = yaml.safe_load(text)
  assert cfg["ingest"]["email"]["sources"][1]["enabled"] is False


def test_set_email_entry_re_enable_flips(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  email:\n"
    "    enabled: true\n"
    "    sources:\n"
    "      - type: emlx\n"
    "        path: ~/x\n"
    "        enabled: false\n"
  )
  cfg_path = _write(tmp_path, body)
  _yaml_set_email_entry_enabled(cfg_path, 0, enabled=True)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  assert cfg["ingest"]["email"]["sources"][0]["enabled"] is True


def test_set_profile_enabled_adds_to_list(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_set_profile_enabled(cfg_path, "calendar", "crayon", enabled=True)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  assert cfg["ingest"]["calendar"]["profiles"] == ["swon", "dno", "crayon"]


def test_set_profile_enabled_removes_from_list(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path)
  _yaml_set_profile_enabled(cfg_path, "calendar", "swon", enabled=False)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  assert cfg["ingest"]["calendar"]["profiles"] == ["dno"]


def test_set_profile_enabled_handles_inline_flow_list(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  calendar:\n"
    "    enabled: false\n"
    "    profiles: ['brkh', 'crayon', 'dno', 'nocos', 'swon']\n"
  )
  cfg_path = _write(tmp_path, body)
  _yaml_set_profile_enabled(cfg_path, "calendar", "kova", enabled=True)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  assert cfg["ingest"]["calendar"]["profiles"] == [
    "brkh", "crayon", "dno", "nocos", "swon", "kova",
  ]


def test_set_profile_enabled_removes_from_inline_flow_list(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  calendar:\n"
    "    enabled: false\n"
    "    profiles: ['brkh', 'crayon', 'swon']\n"
  )
  cfg_path = _write(tmp_path, body)
  _yaml_set_profile_enabled(cfg_path, "calendar", "crayon", enabled=False)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  assert cfg["ingest"]["calendar"]["profiles"] == ["brkh", "swon"]


def test_set_profile_enabled_lazy_creates_mail_block(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  imessage:\n"
    "    enabled: true\n"
    "    chat_db_path: ~/Library/Messages/chat.db\n"
  )
  cfg_path = _write(tmp_path, body)
  _yaml_set_profile_enabled(cfg_path, "mail", "crayon", enabled=True)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  mail = cfg["ingest"]["mail"]
  assert mail["enabled"] is False
  assert mail["profiles"] == ["crayon"]
  assert mail["folders"] == ["Inbox", "SentItems"]
  assert mail["chunk_days"] == 30


def test_rewrite_enabled_flags_lazy_creates_mail_block(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  imessage:\n"
    "    enabled: true\n"
    "    chat_db_path: ~/Library/Messages/chat.db\n"
  )
  cfg_path = _write(tmp_path, body)
  changed = _rewrite_enabled_flags(cfg_path, {"mail": True})
  assert changed == {"mail": True}
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  assert cfg["ingest"]["mail"]["enabled"] is True
  assert cfg["ingest"]["mail"]["profiles"] == []


def test_build_rows_synthesizes_missing_m365_from_piggy(
  tmp_path: Path, monkeypatch
) -> None:
  body = (
    "ingest:\n"
    "  imessage:\n"
    "    enabled: true\n"
    "    chat_db_path: ~/Library/Messages/chat.db\n"
  )
  _stub_discovery(
    monkeypatch,
    teams=[
      {"alias": "crayon", "enabled": True, "default": True},
      {"alias": "brkh", "enabled": True, "default": False},
    ],
  )
  cfg_path = _write(tmp_path, body)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  source_names = [r.name for r in rows if isinstance(r, SourceRow)]
  assert "mail" in source_names
  assert "calendar" in source_names
  assert "teams" in source_names
  mail_children = [
    r for r in rows
    if isinstance(r, SubPathRow) and r.parent == "mail"
  ]
  assert {c.label for c in mail_children} == {"crayon", "brkh"}
  assert all(c.enabled is False for c in mail_children)


def test_build_rows_filters_synthetic_profiles_by_type(
  tmp_path: Path, monkeypatch
) -> None:
  body = (
    "ingest:\n"
    "  imessage:\n"
    "    enabled: true\n"
    "    chat_db_path: ~/Library/Messages/chat.db\n"
  )
  _stub_discovery(
    monkeypatch,
    calendar=[
      {"alias": "crayon", "kind": "oauth", "default": True},
      {"alias": "brkh-g", "kind": "oauth", "default": False},
    ],
    teams=[
      {"alias": "crayon", "type": "m365", "enabled": True, "default": True},
      {"alias": "brkh-g", "type": "google", "enabled": True, "default": False},
      {"alias": "nc-ado", "type": "ado", "enabled": True, "default": False},
    ],
  )
  cfg_path = _write(tmp_path, body)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  # google/ado profiles feed none of mail/calendar/teams — only crayon shows.
  for parent in ("mail", "calendar", "teams", "teams_channels"):
    children = {
      r.label for r in rows
      if isinstance(r, SubPathRow) and r.parent == parent
    }
    assert children == {"crayon"}, f"{parent}: {children}"


def test_build_rows_no_type_defaults_to_m365(tmp_path: Path, monkeypatch) -> None:
  # Older owa-piggy without a `type` field: every profile stays visible.
  body = (
    "ingest:\n"
    "  imessage:\n"
    "    enabled: true\n"
    "    chat_db_path: ~/Library/Messages/chat.db\n"
  )
  _stub_discovery(
    monkeypatch,
    teams=[
      {"alias": "crayon", "enabled": True, "default": True},
      {"alias": "brkh-g", "enabled": True, "default": False},
    ],
  )
  cfg_path = _write(tmp_path, body)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  teams_children = {
    r.label for r in rows if isinstance(r, SubPathRow) and r.parent == "teams"
  }
  assert teams_children == {"crayon", "brkh-g"}


def test_build_rows_skips_m365_synthesis_when_already_configured(
  tmp_path: Path, monkeypatch
) -> None:
  _stub_discovery(
    monkeypatch,
    teams=[{"alias": "swon", "enabled": True}],
  )
  cfg_path = _write(tmp_path)  # SAMPLE has calendar + teams blocks
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  synthetic = [
    r for r in rows
    if isinstance(r, SourceRow) and r.name in {"calendar", "teams"} and r.synthetic
  ]
  assert synthetic == []


def test_build_rows_skips_m365_synthesis_when_no_piggy_profiles(
  tmp_path: Path, monkeypatch
) -> None:
  body = (
    "ingest:\n"
    "  imessage:\n"
    "    enabled: true\n"
    "    chat_db_path: ~/Library/Messages/chat.db\n"
  )
  _stub_discovery(monkeypatch, teams=[])
  cfg_path = _write(tmp_path, body)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  source_names = [r.name for r in rows if isinstance(r, SourceRow)]
  assert "mail" not in source_names
  assert "calendar" not in source_names
  assert "teams" not in source_names


def test_set_profile_enabled_creates_profiles_key(tmp_path: Path) -> None:
  body = (
    "ingest:\n"
    "  calendar:\n"
    "    enabled: false\n"
  )
  cfg_path = _write(tmp_path, body)
  _yaml_set_profile_enabled(cfg_path, "calendar", "swon", enabled=True)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  assert cfg["ingest"]["calendar"]["profiles"] == ["swon"]


def test_build_rows_emits_subpaths(tmp_path: Path, monkeypatch) -> None:
  _stub_discovery(monkeypatch)
  cfg_path = _write(tmp_path)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)

  email_idx = next(
    i for i, r in enumerate(rows) if isinstance(r, SourceRow) and r.name == "email"
  )
  assert isinstance(rows[email_idx + 1], SubPathRow)
  assert isinstance(rows[email_idx + 2], SubPathRow)

  folders_idx = next(
    i for i, r in enumerate(rows) if isinstance(r, SourceRow) and r.name == "folders"
  )
  child = rows[folders_idx + 1]
  assert isinstance(child, SubPathRow)
  assert child.label == "~/Documents/notes"
  assert child.enabled is True


def test_build_rows_marks_disabled_folder_entry(tmp_path: Path, monkeypatch) -> None:
  _stub_discovery(monkeypatch)
  body = (
    "ingest:\n"
    "  folders:\n"
    "    enabled: true\n"
    "    paths:\n"
    "      - path: ~/a\n"
    "        enabled: false\n"
    "      - ~/b\n"
  )
  cfg_path = _write(tmp_path, body)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  folder_children = [r for r in rows if isinstance(r, SubPathRow) and r.parent == "folders"]
  assert [(c.label, c.enabled) for c in folder_children] == [("~/a", False), ("~/b", True)]


def test_build_rows_renders_calendar_profiles_from_discovery(
  tmp_path: Path, monkeypatch
) -> None:
  _stub_discovery(monkeypatch, calendar=[
    {"alias": "swon", "kind": "oauth", "default": True},
    {"alias": "dno", "kind": "oauth", "default": False},
    {"alias": "crayon", "kind": "oauth", "default": False},
    {"alias": "kova", "kind": "webcal", "default": False},
  ])
  cfg_path = _write(tmp_path)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)

  cal_children = [r for r in rows if isinstance(r, SubPathRow) and r.parent == "calendar"]
  by_label = {c.label: c for c in cal_children}
  assert set(by_label) == {"swon", "dno", "crayon", "kova"}
  assert by_label["swon"].enabled is True
  assert by_label["dno"].enabled is True
  assert by_label["crayon"].enabled is False
  assert by_label["kova"].enabled is False
  assert "default" in by_label["swon"].tag
  assert "webcal" in by_label["kova"].tag


def test_build_rows_synthesizes_folders_when_missing(tmp_path: Path, monkeypatch) -> None:
  _stub_discovery(monkeypatch)
  cfg_path = _write(tmp_path, SAMPLE_NO_FOLDERS)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  folders_row = next(
    r for r in rows if isinstance(r, SourceRow) and r.name == "folders"
  )
  assert folders_row.synthetic is True
  assert "0 source(s)" in folders_row.summary


def test_discover_calendar_handles_missing_cli(monkeypatch) -> None:
  def fake_run(cmd, *args, **kwargs):
    raise FileNotFoundError(cmd[0])
  monkeypatch.setattr(sources_mod.subprocess, "run", fake_run)
  sources_mod._clear_profile_cache()
  assert sources_mod.discover_calendar_profiles() == []


def test_discover_calendar_parses_json(monkeypatch) -> None:
  class Result:
    returncode = 0
    stdout = '[{"alias":"swon","kind":"oauth","default":true}]'

  monkeypatch.setattr(sources_mod.subprocess, "run", lambda *a, **kw: Result())
  sources_mod._clear_profile_cache()
  result = sources_mod.discover_calendar_profiles()
  assert result == [{"alias": "swon", "kind": "oauth", "default": True}]


def test_discover_teams_marks_unregistered_profiles_disabled(monkeypatch) -> None:
  class Result:
    returncode = 0
    stdout = (
      '{"profiles":['
      '{"alias":"swon","default":true,"registered":true,"has_config":true},'
      '{"alias":"crayon","default":false,"registered":false,"has_config":true}'
      ']}'
    )

  monkeypatch.setattr(sources_mod.subprocess, "run", lambda *a, **kw: Result())
  sources_mod._clear_profile_cache()
  result = sources_mod.discover_teams_profiles()
  by_alias = {p["alias"]: p for p in result}
  assert by_alias["swon"]["enabled"] is True
  assert by_alias["crayon"]["enabled"] is False
