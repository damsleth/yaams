"""The entity dictionary store: a JSON file living next to the database.

The entity dictionary (curated canonical names + aliases used to seed NER and
the alias tagger) used to live inline in ``config.yaml`` under ``entities:
dictionary:``. That coupled a growing *data* list to a hand-edited *config*
file and — because it was persisted by regex-splicing the YAML text — was prone
to appending duplicate ``entities:`` blocks on every write.

It now lives in its own JSON file (``entities.json`` next to ``data.db`` by
default, or wherever ``entities.dictionary_path`` points). Writes are a clean
full-overwrite, so duplication is impossible by construction. ``load_config``
reads this file transparently into ``cfg["entities"]["dictionary"]``, so every
downstream consumer (the tagger, ``seed_entities``, the ``entities`` CLI) is
unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yaams.config import get_db_path

DEFAULT_STORE_NAME = "entities.json"


def store_path(config: dict[str, Any]) -> Path:
  """Resolve the JSON entity-store path for a loaded config.

  Uses ``entities.dictionary_path`` when set; a relative value is resolved
  next to the database so the store travels with the data it describes.
  Falls back to ``<db dir>/entities.json``.
  """
  entities = config.get("entities")
  raw = entities.get("dictionary_path") if isinstance(entities, dict) else None
  db_dir = get_db_path(config).parent
  if raw:
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (db_dir / p)
  return db_dir / DEFAULT_STORE_NAME


def _coerce_entries(data: Any, path: Path) -> list[dict]:
  """Accept a bare JSON array, or an object wrapping it under ``entities`` /
  ``dictionary``. Returns only well-formed entries (a dict with a canonical)."""
  if isinstance(data, dict):
    for key in ("entities", "dictionary"):
      if key in data:
        data = data[key]
        break
  if not isinstance(data, list):
    raise ValueError(
      f"Entity store must be a JSON array of entries (or an object with an "
      f"'entities' array): {path}"
    )
  return [e for e in data if isinstance(e, dict) and str(e.get("canonical", "")).strip()]


def load_dictionary(config: dict[str, Any]) -> list[dict]:
  """Load the entity dictionary from the JSON store, or ``[]`` if absent."""
  path = store_path(config)
  if not path.is_file():
    return []
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (json.JSONDecodeError, OSError) as exc:
    raise ValueError(f"Entity store is not readable JSON: {path}: {exc}") from exc
  return _coerce_entries(data, path)


def save_dictionary(config: dict[str, Any], dictionary: list[dict]) -> Path:
  """Atomically write the dictionary to the JSON store as a clean array.

  Full overwrite via a temp file + rename, so a write can never duplicate or
  half-truncate the store. Returns the path written.
  """
  path = store_path(config)
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = json.dumps(list(dictionary), ensure_ascii=False, indent=2) + "\n"
  tmp = path.with_name(path.name + ".tmp")
  tmp.write_text(payload, encoding="utf-8")
  tmp.replace(path)
  return path


def _clean_aliases(aliases: list[Any], canonical: str) -> list[str]:
  """De-duplicate aliases case-insensitively (first spelling wins), preserving
  order and dropping blanks and any alias equal to the canonical name."""
  seen = {canonical.casefold()}
  out: list[str] = []
  for raw in aliases:
    alias = str(raw).strip()
    if not alias:
      continue
    key = alias.casefold()
    if key in seen:
      continue
    seen.add(key)
    out.append(alias)
  return out


def dedupe_dictionary(entries: list[dict]) -> tuple[list[dict], dict]:
  """Collapse duplicate entries and clean up aliases.

  Entries are grouped by casefolded canonical name; the first occurrence keeps
  the canonical spelling and type, and any later duplicates fold their aliases
  into it. Aliases are de-duplicated case-insensitively and an alias identical
  to its canonical is dropped. Conservative on purpose — only entries that
  share a canonical are merged, never distinct names — so it is safe to run
  unattended as part of ``yaams ingest``.

  Returns ``(deduped_entries, {"dropped": int, "aliases_merged": int})``.
  """
  out: list[dict] = []
  by_key: dict[str, dict] = {}
  dropped = 0
  aliases_merged = 0

  for entry in entries:
    if not isinstance(entry, dict):
      continue
    canonical = str(entry.get("canonical", "")).strip()
    if not canonical:
      continue
    key = canonical.casefold()
    incoming_aliases = list(entry.get("aliases") or [])

    survivor = by_key.get(key)
    if survivor is None:
      new_entry: dict = {"canonical": canonical}
      etype = entry.get("type")
      if etype is not None:
        new_entry["type"] = etype
      cleaned = _clean_aliases(incoming_aliases, canonical)
      if cleaned:
        new_entry["aliases"] = cleaned
      by_key[key] = new_entry
      out.append(new_entry)
      continue

    # Duplicate canonical: fold its aliases into the survivor.
    dropped += 1
    before = len(survivor.get("aliases") or [])
    combined = list(survivor.get("aliases") or []) + incoming_aliases
    cleaned = _clean_aliases(combined, survivor["canonical"])
    if cleaned:
      survivor["aliases"] = cleaned
    elif "aliases" in survivor:
      del survivor["aliases"]
    aliases_merged += max(0, len(survivor.get("aliases") or []) - before)

  return out, {"dropped": dropped, "aliases_merged": aliases_merged}
