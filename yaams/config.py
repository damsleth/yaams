from __future__ import annotations

from pathlib import Path
from typing import Any


def expand_path(value: str | Path) -> Path:
  return Path(value).expanduser().resolve()


def load_config(path: str | Path) -> dict[str, Any]:
  try:
    import yaml
  except ImportError as exc:
    raise RuntimeError("PyYAML is required to read YAAMS config files") from exc

  config_path = expand_path(path)
  data = yaml.safe_load(config_path.read_text()) or {}
  if not isinstance(data, dict):
    raise ValueError(f"Config file must contain a mapping: {config_path}")
  return data


def get_db_path(config: dict[str, Any]) -> Path:
  raw_path = config.get("db_path")
  if not raw_path:
    raise ValueError("Config is missing db_path")
  return expand_path(raw_path)

