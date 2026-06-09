"""`yaams init` writes a default config from the shipped example.

Action-class command per the YAAMS CLI conventions - emits an envelope
under --json, plain output otherwise, and refuses to clobber an
existing file unless --force is passed.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from yaams.cli import cli


def test_init_writes_default_config(monkeypatch, tmp_path: Path):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  result = CliRunner().invoke(cli, ["init"])
  assert result.exit_code == 0, result.output

  dest = tmp_path / "yaams" / "config.yaml"
  assert dest.is_file()
  body = dest.read_text()
  # Sanity-check: the default example header should be present.
  assert "YAAMS configuration" in body
  # And a couple of expected sections.
  assert "db_path:" in body
  assert "ingest:" in body


def test_init_json_envelope_shape(monkeypatch, tmp_path: Path):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  result = CliRunner().invoke(cli, ["init", "--json"])
  assert result.exit_code == 0
  doc = json.loads(result.output)
  assert doc["tool"] == "yaams"
  assert doc["command"] == "init"
  assert doc["ok"] is True
  assert doc["error"] is None
  assert doc["stats"]["path"].endswith("/yaams/config.yaml")


def test_init_refuses_to_overwrite_without_force(monkeypatch, tmp_path: Path):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  dest = tmp_path / "yaams" / "config.yaml"
  dest.parent.mkdir(parents=True)
  dest.write_text("# user-curated\nkeep: this\n")
  original = dest.read_bytes()

  result = CliRunner().invoke(cli, ["init", "--json"])
  assert result.exit_code != 0
  doc = json.loads(result.output)
  assert doc["ok"] is False
  assert doc["error"]["code"] == "exists"

  # File unchanged.
  assert dest.read_bytes() == original


def test_init_force_overwrites(monkeypatch, tmp_path: Path):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  dest = tmp_path / "yaams" / "config.yaml"
  dest.parent.mkdir(parents=True)
  dest.write_text("# user-curated\n")

  result = CliRunner().invoke(cli, ["init", "--force"])
  assert result.exit_code == 0, result.output
  assert "user-curated" not in dest.read_text()
  assert "YAAMS configuration" in dest.read_text()


def test_init_custom_path(monkeypatch, tmp_path: Path):
  custom = tmp_path / "elsewhere" / "my-yaams.yaml"
  result = CliRunner().invoke(cli, ["init", "--path", str(custom), "--json"])
  assert result.exit_code == 0
  assert custom.is_file()
  doc = json.loads(result.output)
  assert doc["stats"]["path"] == str(custom)
