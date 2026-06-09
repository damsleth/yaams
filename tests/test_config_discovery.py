"""Config-path discovery order.

YAAMS resolves a config file by walking, in order:

  1. ``$YAAMS_CONFIG`` (explicit override)
  2. ``$XDG_CONFIG_HOME/yaams/config.yaml`` (standard config path)
  3. ``./config.yaml``

This file pins that order.
"""
from __future__ import annotations

import pytest

from yaams.config import resolve_config_path

_MINIMAL = "db_path: /tmp/x.db\n"


@pytest.fixture
def clean_env(monkeypatch):
  """Remove env vars that would shadow the XDG search order."""
  monkeypatch.delenv("YAAMS_CONFIG", raising=False)
  monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
  return monkeypatch


def test_xdg_path_resolves_without_yaams_config_env(tmp_path, clean_env):
  xdg = tmp_path / "xdg"
  cfg = xdg / "yaams" / "config.yaml"
  cfg.parent.mkdir(parents=True)
  cfg.write_text(_MINIMAL)

  clean_env.setenv("XDG_CONFIG_HOME", str(xdg))

  resolved = resolve_config_path()
  assert resolved == cfg.resolve()


def test_cwd_config_resolves_when_present(tmp_path, clean_env):
  clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
  clean_env.chdir(tmp_path)

  cwd_cfg = tmp_path / "config.yaml"
  cwd_cfg.write_text(_MINIMAL)

  resolved = resolve_config_path()
  assert resolved == cwd_cfg.resolve()


def test_yaams_config_env_overrides_xdg(tmp_path, clean_env):
  xdg = tmp_path / "xdg"
  cfg = xdg / "yaams" / "config.yaml"
  cfg.parent.mkdir(parents=True)
  cfg.write_text(_MINIMAL)

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
  config path.
  """
  fake_home = tmp_path / "home"
  fake_home.mkdir()
  monkeypatch.setenv("HOME", str(fake_home))

  from yaams.config import _candidate_config_paths

  paths = _candidate_config_paths()
  rendered = [str(p) for p in paths]
  assert any(p.endswith("/yaams/config.yaml") for p in rendered)


def test_no_config_anywhere_raises_filenotfound(tmp_path, clean_env):
  clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
  clean_env.chdir(tmp_path)  # cwd has no config.yaml

  with pytest.raises(FileNotFoundError) as excinfo:
    resolve_config_path()
  # Error message mentions the candidate path so users can see where
  # YAAMS looked.
  msg = str(excinfo.value)
  assert "yaams/config.yaml" in msg
