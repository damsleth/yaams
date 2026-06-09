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
  2. ``$XDG_CONFIG_HOME/yaams/config.yaml`` - the standard config path.
  3. ``./config.yaml`` - cwd fallback for ad-hoc / dev usage.

  When ``XDG_CONFIG_HOME`` is unset, ``~/.config`` is used per the
  XDG Base Directory spec.
  """
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
    "\nCreate a config at $XDG_CONFIG_HOME/yaams/config.yaml, "
    "or set $YAAMS_CONFIG to point at one."
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
  _validate_config(data, config_path)
  _load_entity_store(data)
  return data


def _load_entity_store(data: dict[str, Any]) -> None:
  """Merge the JSON entity store into ``data["entities"]["dictionary"]``.

  No-op unless a db_path is set and the store file actually exists, so configs
  with an inline ``entities.dictionary`` (pre-migration / tests) are untouched
  until the store is created. When the store exists it is the source of truth
  and overrides any stale inline list.
  """
  if not data.get("db_path"):
    return
  # Lazy import: entities_store imports from this module, so importing it at
  # module load would create a cycle.
  from yaams.entities_store import load_dictionary, store_path

  if not store_path(data).is_file():
    return
  entities = data.get("entities")
  if not isinstance(entities, dict):
    entities = {}
    data["entities"] = entities
  entities["dictionary"] = load_dictionary(data)


# Numeric knobs that get coerced with int()/float() deep in the call stack.
# Validating them up front turns an opaque ValueError/TypeError mid-ingest
# into a clear "fix this key" message at load time. Each entry is
# (dotted path, expected python type, must-be-positive).
_NUMERIC_KNOBS: tuple[tuple[str, type | tuple[type, ...], bool], ...] = (
  ("embed.batch_size", int, True),
  ("embed.dimension", int, True),
  ("synth.timeout", (int, float), True),
  ("ingest.mail.chunk_days", int, True),
  ("ingest.mail.page_size", int, True),
  ("ingest.teams.page_size", int, True),
  ("ingest.teams_channels.limit_pages", int, True),
  ("ingest.teams_channels.max_retries", int, False),
  ("ingest.calendar.chunk_days", int, True),
)


def _dig(data: dict[str, Any], dotted: str) -> tuple[bool, Any]:
  """Return (found, value) for a dotted path, stopping if a node isn't a dict."""
  node: Any = data
  for part in dotted.split("."):
    if not isinstance(node, dict) or part not in node:
      return False, None
    node = node[part]
  return True, node


def _validate_config(data: dict[str, Any], config_path: Path) -> None:
  """Reject structurally broken configs with an actionable message.

  Catches the two failure modes that otherwise surface as cryptic
  tracebacks well after load: a top-level section that isn't a mapping,
  and a numeric knob set to a non-numeric (or non-positive) value.
  """
  for section in ("ingest", "embed", "synth", "entities"):
    value = data.get(section)
    if value is not None and not isinstance(value, dict):
      raise ValueError(
        f"Config section '{section}' must be a mapping, got "
        f"{type(value).__name__}: {config_path}"
      )

  for dotted, expected, positive in _NUMERIC_KNOBS:
    found, value = _dig(data, dotted)
    if not found or value is None:
      continue
    # bool is a subclass of int; reject it explicitly as a likely mistake.
    if isinstance(value, bool) or not isinstance(value, expected):
      raise ValueError(
        f"Config value '{dotted}' must be a number, got "
        f"{type(value).__name__} ({value!r}): {config_path}"
      )
    if positive and value <= 0:
      raise ValueError(
        f"Config value '{dotted}' must be positive, got {value!r}: {config_path}"
      )


def _apply_aliases(data: dict[str, Any]) -> None:
  """Rewrite suite-wide config aliases in place.

  `ingest.ledger:` is a user-facing alias
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
