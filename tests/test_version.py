from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from click.testing import CliRunner

import yaams
from yaams.cli import cli


def test_version_attribute_is_semver():
  assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.+-].+)?", yaams.__version__)


def test_version_matches_pyproject():
  pyproject = tomllib.loads(
    Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text()
  )
  assert pyproject["project"]["version"] == yaams.__version__


def test_cli_version_flag():
  result = CliRunner().invoke(cli, ["--version"])
  assert result.exit_code == 0
  assert yaams.__version__ in result.output


def test_version_command_human():
  result = CliRunner().invoke(cli, ["version"])
  assert result.exit_code == 0
  assert result.output.strip() == f"yaams {yaams.__version__}"


def test_version_command_json():
  result = CliRunner().invoke(cli, ["version", "--json"])
  assert result.exit_code == 0
  payload = json.loads(result.output)
  assert payload == {"tool": "yaams", "version": yaams.__version__}
