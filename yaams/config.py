from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def expand_path(value: str | Path) -> Path:
  return Path(value).expanduser().resolve()


def _candidate_config_paths() -> list[Path]:
  candidates: list[Path] = []

  env_path = os.environ.get("YAAMS_CONFIG")
  if env_path:
    candidates.append(expand_path(env_path))

  xdg = os.environ.get("XDG_CONFIG_HOME")
  xdg_root = expand_path(xdg) if xdg else expand_path("~/.config")
  candidates.append(xdg_root / "yaams" / "config.yaml")

  candidates.append(expand_path("config.yaml"))

  return candidates


def resolve_config_path(explicit: str | Path | None = None) -> Path:
  if explicit is not None:
    return expand_path(explicit)

  for candidate in _candidate_config_paths():
    if candidate.is_file():
      return candidate

  searched = "\n  ".join(str(p) for p in _candidate_config_paths())
  raise FileNotFoundError(
    "Could not find a YAAMS config file. Searched:\n  " + searched +
    "\nSet $YAAMS_CONFIG or place a file at ~/.config/yaams/config.yaml."
  )


def load_config(path: str | Path | None = None) -> dict[str, Any]:
  try:
    import yaml
  except ImportError as exc:
    raise RuntimeError("PyYAML is required to read YAAMS config files") from exc

  config_path = resolve_config_path(path)
  data = yaml.safe_load(config_path.read_text()) or {}
  if not isinstance(data, dict):
    raise ValueError(f"Config file must contain a mapping: {config_path}")
  return data


def get_db_path(config: dict[str, Any]) -> Path:
  raw_path = config.get("db_path")
  if not raw_path:
    raise ValueError("Config is missing db_path")
  return expand_path(raw_path)
