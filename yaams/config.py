from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def expand_path(value: str | Path) -> Path:
  return Path(value).expanduser().resolve()


def _candidate_config_paths() -> list[Path]:
  """Build the ordered list of config paths YAAMS searches.

  Order (first match wins):

  1. ``$YAAMS_CONFIG`` - explicit override.
  2. ``$XDG_CONFIG_HOME/hugr/yaams/config.yaml`` - the suite path. This
     is where ``hugr init`` writes the generated YAAMS config, so it
     wins over the legacy direct-CLI location when both files exist.
  3. ``$XDG_CONFIG_HOME/yaams/config.yaml`` - legacy direct-CLI path,
     kept so existing users who installed YAAMS standalone keep
     working.
  4. ``./config.yaml`` - cwd fallback for ad-hoc / dev usage.

  When ``XDG_CONFIG_HOME`` is unset, ``~/.config`` is used per the
  XDG Base Directory spec.
  """
  candidates: list[Path] = []

  env_path = os.environ.get("YAAMS_CONFIG")
  if env_path:
    candidates.append(expand_path(env_path))

  xdg = os.environ.get("XDG_CONFIG_HOME")
  xdg_root = expand_path(xdg) if xdg else expand_path("~/.config")
  # Suite path first (hugr init writes here), then legacy direct-CLI
  # path. See docstring above for rationale.
  candidates.append(xdg_root / "hugr" / "yaams" / "config.yaml")
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
    "\nRun `hugr init` to generate one, or set $YAAMS_CONFIG."
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
  _apply_aliases(data)
  return data


def _apply_aliases(data: dict[str, Any]) -> None:
  """Rewrite suite-wide config aliases in place.

  Per hugr CONVENTIONS.md, `ingest.ledger:` is a user-facing alias
  for the internal `ingest.tier2_ledger:` block. The internal source
  id stays `tier2_ledger`; we accept the friendlier name on input.

  If both keys are present, the explicit `tier2_ledger` block wins
  (canonical key takes priority over its alias).
  """
  ingest = data.get("ingest")
  if not isinstance(ingest, dict):
    return
  if "ledger" in ingest and "tier2_ledger" not in ingest:
    ingest["tier2_ledger"] = ingest.pop("ledger")
  elif "ledger" in ingest and "tier2_ledger" in ingest:
    # Both forms present - canonical wins. Drop the alias quietly to
    # avoid two parallel sub-trees being kept around.
    ingest.pop("ledger")


def get_db_path(config: dict[str, Any]) -> Path:
  raw_path = config.get("db_path")
  if not raw_path:
    raise ValueError("Config is missing db_path")
  return expand_path(raw_path)
