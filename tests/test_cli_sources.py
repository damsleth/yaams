from __future__ import annotations

from pathlib import Path

from yaams.cli.sources import (
  SourceRow,
  SubPathRow,
  _build_rows,
  _rewrite_enabled_flags,
  _yaml_append_email_source,
  _yaml_append_folder_path,
  _yaml_remove_email_source,
  _yaml_remove_folder_path,
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


def test_build_rows_emits_subpaths(tmp_path: Path) -> None:
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


def test_build_rows_synthesizes_folders_when_missing(tmp_path: Path) -> None:
  cfg_path = _write(tmp_path, SAMPLE_NO_FOLDERS)
  import yaml
  cfg = yaml.safe_load(cfg_path.read_text())
  rows = _build_rows(cfg)
  folders_row = next(
    r for r in rows if isinstance(r, SourceRow) and r.name == "folders"
  )
  assert folders_row.synthetic is True
  assert "0 source(s)" in folders_row.summary
