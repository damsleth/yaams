from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import platform
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yaams.config import load_config


DEFAULT_CONFIG = {
  "db_path": "~/yaams/data.db",
  "ingest": {
    "since": "2025-01-01T00:00:00Z",
    "imessage": {
      "enabled": True,
      "chat_db_path": "~/Library/Messages/chat.db",
    },
    "email": {
      "enabled": True,
      "sources": [
        {
          "type": "emlx",
          "path": "~/Library/Mail/V10",
        }
      ],
    },
  },
  "embed": {
    "model": "BAAI/bge-m3",
    "batch_size": 32,
    "device": "mps",
    "dimension": 1024,
  },
  "entities": {
    "spacy_model": "xx_ent_wiki_sm",
    "dictionary": [
      # Example entries - replace with the names, places, and projects you
      # actually want to track. The wizard will prompt you to edit this.
      {
        "canonical": "Example Person",
        "type": "person",
        "aliases": ["EP", "Ex Person"],
      },
      {
        "canonical": "Example Org",
        "type": "org",
        "aliases": ["EO"],
      },
    ],
  },
}


def main() -> None:
  parser = argparse.ArgumentParser(description="Configure YAAMS Phase A ingest")
  parser.add_argument("--config", default="config.yaml")
  parser.add_argument(
    "--defaults",
    action="store_true",
    help="write existing/default config without prompting",
  )
  args = parser.parse_args()

  config_path = Path(args.config).expanduser()
  cfg = _load_existing_or_default(config_path)
  cfg = _with_detected_defaults(cfg)

  if args.defaults or not sys.stdin.isatty():
    _write_config(config_path, cfg)
    print_config_summary(config_path, cfg)
    return

  print()
  print("YAAMS Phase A configuration")
  print("---------------------------")
  print("Press Enter to accept the shown default.")
  print()

  cfg["db_path"] = prompt_text("SQLite database path", cfg["db_path"])
  cfg["ingest"]["since"] = prompt_text(
    "Earliest item timestamp",
    cfg["ingest"]["since"],
  )

  imessage = cfg["ingest"]["imessage"]
  imessage["enabled"] = prompt_bool("Enable iMessage ingest", imessage["enabled"])
  if imessage["enabled"]:
    imessage["chat_db_path"] = prompt_text(
      "Messages chat.db path",
      imessage["chat_db_path"],
    )

  email_cfg = cfg["ingest"]["email"]
  email_cfg["enabled"] = prompt_bool("Enable email ingest", email_cfg["enabled"])
  if email_cfg["enabled"]:
    email_cfg["sources"] = prompt_email_sources(email_cfg.get("sources", []))

  embed = cfg["embed"]
  embed["device"] = prompt_choice(
    "Embedding device",
    str(embed.get("device", detected_device_default())),
    ["mps", "cpu", "cuda"],
  )
  embed["model"] = prompt_text("Embedding model", embed["model"])

  entities = cfg["entities"]
  print()
  print(f"Entity dictionary has {len(entities.get('dictionary', []))} entries.")
  if prompt_bool("Add more entities now", False):
    entities["dictionary"] = list(entities.get("dictionary", []))
    add_entities(entities["dictionary"])

  print()
  if prompt_bool(f"Write {config_path}", True):
    _write_config(config_path, cfg)
    print_config_summary(config_path, cfg)
  else:
    print("Config not written.")


def prompt_email_sources(existing: list[dict]) -> list[dict]:
  current = existing[0] if existing else {"type": "emlx", "path": "~/Library/Mail/V10"}
  current_type = str(current.get("type", "emlx"))
  current_path = str(current.get("path", "~/Library/Mail/V10"))
  default_kind = "apple-mail" if current_type == "emlx" else current_type

  print()
  print("Email source options:")
  print("  apple-mail: native Mail.app .emlx store")
  print("  mbox: exported mailbox file")
  print("  none: disable email ingest")
  kind = prompt_choice("Email source", default_kind, ["apple-mail", "mbox", "none"])
  if kind == "none":
    return []
  if kind == "apple-mail":
    path = prompt_text("Apple Mail store path", current_path or "~/Library/Mail/V10")
    return [{"type": "emlx", "path": path}]
  path = prompt_text("mbox file path", current_path if current_type == "mbox" else "")
  return [{"type": "mbox", "path": path}]


def add_entities(dictionary: list[dict]) -> None:
  while True:
    print()
    canonical = prompt_text("Canonical entity name", "")
    if not canonical:
      break
    entity_type = prompt_choice(
      "Entity type",
      "person",
      ["person", "place", "project", "org", "other"],
    )
    aliases_raw = prompt_text("Aliases, comma-separated", "")
    aliases = [alias.strip() for alias in aliases_raw.split(",") if alias.strip()]
    dictionary.append(
      {
        "canonical": canonical,
        "type": entity_type,
        "aliases": aliases,
      }
    )
    if not prompt_bool("Add another entity", False):
      break


def prompt_text(label: str, default: str) -> str:
  suffix = f" [{default}]" if default else ""
  value = input(f"{label}{suffix}: ").strip()
  return value or default


def prompt_bool(label: str, default: bool) -> bool:
  suffix = "Y/n" if default else "y/N"
  while True:
    value = input(f"{label} [{suffix}]: ").strip().lower()
    if not value:
      return default
    if value in {"y", "yes"}:
      return True
    if value in {"n", "no"}:
      return False
    print("Please answer y or n.")


def prompt_choice(label: str, default: str, choices: list[str]) -> str:
  choices_text = "/".join(choices)
  while True:
    value = input(f"{label} [{default}] ({choices_text}): ").strip().lower()
    value = value or default
    if value in choices:
      return value
    print(f"Choose one of: {choices_text}")


def print_config_summary(config_path: Path, cfg: dict) -> None:
  print()
  print(f"Wrote config: {config_path}")
  print(f"  db_path: {cfg['db_path']}")
  print(f"  since: {cfg['ingest']['since']}")
  print(
    "  imessage: "
    f"{'enabled' if cfg['ingest']['imessage']['enabled'] else 'disabled'} "
    f"({cfg['ingest']['imessage']['chat_db_path']})"
  )
  email = cfg["ingest"]["email"]
  if email["enabled"] and email.get("sources"):
    for source in email["sources"]:
      print(f"  email: {source['type']} ({source['path']})")
  else:
    print("  email: disabled")
  print(f"  embed.device: {cfg['embed']['device']}")
  print(f"  entities: {len(cfg['entities'].get('dictionary', []))} dictionary entries")


def _load_existing_or_default(config_path: Path) -> dict:
  cfg = deepcopy(DEFAULT_CONFIG)
  if config_path.exists():
    cfg = deep_merge(cfg, load_config(config_path))
  return cfg


def _with_detected_defaults(cfg: dict) -> dict:
  cfg = deepcopy(cfg)
  imessage_path = Path("~/Library/Messages/chat.db").expanduser()
  mail_path = detected_apple_mail_path()
  cfg["ingest"]["imessage"].setdefault("chat_db_path", str(imessage_path))
  if not imessage_path.exists() and _is_default_value(
    cfg["ingest"]["imessage"].get("enabled"),
    DEFAULT_CONFIG["ingest"]["imessage"]["enabled"],
  ):
    cfg["ingest"]["imessage"]["enabled"] = False

  email = cfg["ingest"]["email"]
  sources = email.setdefault("sources", [])
  if not sources:
    sources.append({"type": "emlx", "path": collapse_home(mail_path)})
  first_source = sources[0]
  if first_source.get("type") == "emlx" and str(first_source.get("path")) in {
    "",
    "~/Library/Mail/V10",
  }:
    first_source["path"] = collapse_home(mail_path)
  first_source_path = Path(str(first_source.get("path", ""))).expanduser()
  if not first_source_path.exists() and _is_default_value(
    email.get("enabled"),
    DEFAULT_CONFIG["ingest"]["email"]["enabled"],
  ):
    email["enabled"] = False

  if _is_default_value(cfg["embed"].get("device"), DEFAULT_CONFIG["embed"]["device"]):
    cfg["embed"]["device"] = detected_device_default()
  return cfg


def detected_device_default() -> str:
  if sys.platform == "darwin" and platform.machine() == "arm64":
    return "mps"
  return "cpu"


def detected_apple_mail_path() -> Path:
  mail_root = Path("~/Library/Mail").expanduser()
  candidates = [path for path in mail_root.glob("V*") if path.is_dir()]
  if not candidates:
    return mail_root / "V10"
  return sorted(candidates, key=mail_version, reverse=True)[0]


def mail_version(path: Path) -> int:
  suffix = path.name.removeprefix("V")
  if suffix.isdigit():
    return int(suffix)
  return -1


def collapse_home(path: Path) -> str:
  home = Path.home()
  try:
    return f"~/{path.relative_to(home)}"
  except ValueError:
    return str(path)


def _is_default_value(value: Any, default: Any) -> bool:
  return value is None or value == default


def deep_merge(base: dict, override: dict) -> dict:
  merged = deepcopy(base)
  for key, value in override.items():
    if isinstance(value, dict) and isinstance(merged.get(key), dict):
      merged[key] = deep_merge(merged[key], value)
    else:
      merged[key] = value
  return merged


def _write_config(config_path: Path, cfg: dict) -> None:
  try:
    import yaml
  except ImportError as exc:
    raise RuntimeError(
      "PyYAML is required to write config files. Run pip install -r requirements.txt first.",
    ) from exc

  config_path.parent.mkdir(parents=True, exist_ok=True)
  config_path.write_text(
    yaml.safe_dump(
      cfg,
      sort_keys=False,
      allow_unicode=True,
      indent=2,
    ),
  )


if __name__ == "__main__":
  main()
