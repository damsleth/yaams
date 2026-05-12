"""Config-path discovery order per mnem suite plan 05.

YAAMS resolves a config file by walking, in order:

  1. ``$YAAMS_CONFIG`` (explicit override)
  2. ``$XDG_CONFIG_HOME/mnem/yaams/config.yaml`` (suite path - where
     ``mnem init`` writes)
  3. ``$XDG_CONFIG_HOME/yaams/config.yaml`` (legacy direct-CLI path)
  4. ``./config.yaml``

This file pins that order. The suite path wins over the legacy path
when both files exist, because ``mnem init`` is the canonical first-
run flow under the mnem suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from yaams.config import resolve_config_path


_MINIMAL = "db_path: /tmp/x.db\n"


@pytest.fixture
def clean_env(monkeypatch):
  """Remove env vars that would shadow the XDG search order."""
  monkeypatch.delenv("YAAMS_CONFIG", raising=False)
  monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
  return monkeypatch


def test_suite_path_resolves_without_yaams_config_env(tmp_path, clean_env):
  xdg = tmp_path / "xdg"
  suite = xdg / "mnem" / "yaams" / "config.yaml"
  suite.parent.mkdir(parents=True)
  suite.write_text(_MINIMAL)

  clean_env.setenv("XDG_CONFIG_HOME", str(xdg))

  resolved = resolve_config_path()
  assert resolved == suite.resolve()


def test_legacy_path_still_resolves_when_only_legacy_exists(tmp_path, clean_env):
  xdg = tmp_path / "xdg"
  legacy = xdg / "yaams" / "config.yaml"
  legacy.parent.mkdir(parents=True)
  legacy.write_text(_MINIMAL)

  clean_env.setenv("XDG_CONFIG_HOME", str(xdg))

  resolved = resolve_config_path()
  assert resolved == legacy.resolve()


def test_suite_path_wins_when_both_exist(tmp_path, clean_env):
  """Decision: suite path wins. ``mnem init`` owns the canonical config
  location under the suite contract; the legacy path is fallback only.
  """
  xdg = tmp_path / "xdg"
  suite = xdg / "mnem" / "yaams" / "config.yaml"
  legacy = xdg / "yaams" / "config.yaml"
  suite.parent.mkdir(parents=True)
  legacy.parent.mkdir(parents=True)
  suite.write_text("db_path: /tmp/suite.db\n")
  legacy.write_text("db_path: /tmp/legacy.db\n")

  clean_env.setenv("XDG_CONFIG_HOME", str(xdg))

  resolved = resolve_config_path()
  assert resolved == suite.resolve()
  assert resolved != legacy.resolve()


def test_yaams_config_env_overrides_both(tmp_path, clean_env):
  xdg = tmp_path / "xdg"
  suite = xdg / "mnem" / "yaams" / "config.yaml"
  suite.parent.mkdir(parents=True)
  suite.write_text(_MINIMAL)

  override = tmp_path / "elsewhere.yaml"
  override.write_text(_MINIMAL)

  clean_env.setenv("XDG_CONFIG_HOME", str(xdg))
  clean_env.setenv("YAAMS_CONFIG", str(override))

  resolved = resolve_config_path()
  assert resolved == override.resolve()


def test_unset_xdg_falls_back_to_dot_config_home(tmp_path, clean_env, monkeypatch):
  """When XDG_CONFIG_HOME is unset, YAAMS uses ~/.config per XDG spec.

  We can't safely write into the real ~/.config in a unit test, so we
  monkeypatch ``Path.home()`` via expand_path's tilde expansion: point
  HOME at a tmpdir and assert the candidate list includes the expected
  suite path.
  """
  fake_home = tmp_path / "home"
  fake_home.mkdir()
  monkeypatch.setenv("HOME", str(fake_home))

  from yaams.config import _candidate_config_paths

  paths = _candidate_config_paths()
  rendered = [str(p) for p in paths]
  # Suite path must precede the legacy path in the search order.
  suite_idx = next(
    i for i, p in enumerate(rendered) if p.endswith("/mnem/yaams/config.yaml")
  )
  legacy_idx = next(
    i for i, p in enumerate(rendered)
    if p.endswith("/yaams/config.yaml") and "/mnem/" not in p
  )
  assert suite_idx < legacy_idx


def test_no_config_anywhere_raises_filenotfound(tmp_path, clean_env):
  clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
  clean_env.chdir(tmp_path)  # cwd has no config.yaml

  with pytest.raises(FileNotFoundError) as excinfo:
    resolve_config_path()
  # Error message mentions both candidate paths so users can see where
  # YAAMS looked.
  msg = str(excinfo.value)
  assert "mnem/yaams/config.yaml" in msg
  assert "yaams/config.yaml" in msg
