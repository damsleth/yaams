"""Import Microsoft 365 people into the entity dictionary.

People (contacts + the org directory) are *reference entities*, not
timestamped events, so they do not belong in the firehose `items` table.
They belong in the entity dictionary that `seed_entities` builds and that
`EntityTagger` reads, so seeding colleagues there makes NER/tagging resolve
them across every source (iMessage, mail, teams, ...) instead of leaving the
dictionary hand-maintained in config.yaml.

This module shells out to `owa-people` (audience: graph, via owa-piggy) and
maps its normalized person records into `{canonical, type, aliases}`
dictionary entries. Every `owa-people` subcommand (`me`, `contacts`,
`directory`, `find`) returns the same shape:

    {"id", "displayName", "email", "jobTitle", "department",
     "companyName", "officeLocation", "mobilePhone", "businessPhones", "source"}

so one mapper covers all of them.

The fetch layer takes an injectable ``runner`` so the subprocess hops can be
faked in tests; the mapping/merge layer is pure.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

# runner(args) -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, str, str]]


def _default_runner(args: list[str]) -> tuple[int, str, str]:
  try:
    proc = subprocess.run(args, capture_output=True, text=True)
  except FileNotFoundError:
    return 127, "", f"{args[0]} not found on PATH"
  return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Mapping (pure).
# ---------------------------------------------------------------------------


def person_to_entry(person: dict, *, etype: str = "person") -> dict | None:
  """Map one normalized owa-people record to a dictionary entry.

  Returns None for records with no usable name: empty, ``unknown``, or a
  bare email address with no real display name (those make terrible NER
  surface forms and would pollute the dictionary).
  """
  name = (person.get("displayName") or "").strip()
  if not name or name.casefold() == "unknown":
    return None
  # A displayName that is itself an email (no spaces) is not a real name.
  if "@" in name and " " not in name:
    return None
  email = (person.get("email") or "").strip()
  entry: dict = {"canonical": name, "type": etype}
  if email and email.casefold() != name.casefold():
    entry["aliases"] = [email]
  return entry


def _union_aliases(existing: list[str], extra: list[str], *, canonical: str) -> list[str]:
  """Case-insensitive union preserving order, dropping the canonical itself."""
  seen = {canonical.casefold()}
  out: list[str] = []
  for value in [*existing, *extra]:
    value = (value or "").strip()
    if not value:
      continue
    key = value.casefold()
    if key in seen:
      continue
    seen.add(key)
    out.append(value)
  return out


def people_to_entries(people: list[dict], *, etype: str = "person") -> list[dict]:
  """Collapse raw person records into unique entries, folding the emails of
  same-named people into one entry's aliases (the directory routinely returns
  the same person under several addresses)."""
  by_key: dict[str, dict] = {}
  order: list[str] = []
  for person in people:
    entry = person_to_entry(person, etype=etype)
    if entry is None:
      continue
    key = entry["canonical"].casefold()
    if key not in by_key:
      by_key[key] = {
        "canonical": entry["canonical"],
        "type": entry["type"],
        "aliases": list(entry.get("aliases") or []),
      }
      order.append(key)
    else:
      acc = by_key[key]
      acc["aliases"] = _union_aliases(
        acc["aliases"], entry.get("aliases") or [], canonical=acc["canonical"]
      )
  result: list[dict] = []
  for key in order:
    entry = by_key[key]
    if not entry["aliases"]:
      entry.pop("aliases")
    result.append(entry)
  return result


def merge_into_dictionary(
  dictionary: list[dict], new_entries: list[dict]
) -> tuple[list[dict], dict]:
  """Union ``new_entries`` into a config entity dictionary, keyed by identity
  rather than canonical name alone.

  A new person is matched to an existing entry by canonical name OR by any
  shared alias (an email already in the dictionary), so a directory record
  with a slightly different displayName but the same address folds into the
  curated entry instead of spawning a duplicate. That matters because the
  tagger's alias index is last-write-wins (yaams/enrich/entities.py): two
  entries sharing an alias would silently misroute that alias to whichever
  entry was built last. An alias already owned by a *different* entry is left
  where it is. Existing matches gain the new name/aliases; nothing is removed
  or renamed. Returns the updated dictionary and
  ``{added, updated, aliases_added}``.
  """
  # Deep-copy alias lists so the caller's dictionary is never mutated.
  out: list[dict] = []
  for entry in dictionary:
    copy = dict(entry)
    if "aliases" in copy:
      copy["aliases"] = list(copy["aliases"] or [])
    out.append(copy)

  # owner maps every known surface form (canonical + each alias, casefolded)
  # to the entry that owns it. First writer wins on pre-existing collisions.
  owner: dict[str, dict] = {}
  for entry in out:
    for name in [entry.get("canonical", ""), *(entry.get("aliases") or [])]:
      owner.setdefault(str(name).casefold(), entry)

  added = updated = aliases_added = 0

  def _attach(target: dict, name: str) -> bool:
    """Add ``name`` as an alias of ``target`` unless it is the target's own
    canonical or already owned by some entry. Returns True if added."""
    key = name.casefold()
    if key == str(target.get("canonical", "")).casefold():
      return False
    if key in owner:
      return False
    target.setdefault("aliases", [])
    target["aliases"].append(name)
    owner[key] = target
    return True

  for new in new_entries:
    canonical = new["canonical"]
    aliases = list(new.get("aliases") or [])
    target = owner.get(canonical.casefold())
    if target is None:
      for alias in aliases:
        target = owner.get(alias.casefold())
        if target is not None:
          break
    if target is None:
      fresh = {"canonical": canonical, "type": new.get("type", "person")}
      if aliases:
        fresh["aliases"] = list(aliases)
      out.append(fresh)
      for name in [canonical, *aliases]:
        owner.setdefault(name.casefold(), fresh)
      added += 1
      continue
    # Matched an existing entry: fold in the new name variant + any new aliases.
    touched = sum(_attach(target, name) for name in [canonical, *aliases])
    if touched:
      aliases_added += touched
      updated += 1
  return out, {"added": added, "updated": updated, "aliases_added": aliases_added}


# ---------------------------------------------------------------------------
# Fetch (subprocess; runner injectable).
# ---------------------------------------------------------------------------


def _invoke(cmd: list[str], runner: Runner) -> tuple[list[dict], str | None]:
  """Run one owa-people command. Returns (records, error_message)."""
  rc, out, err = runner(cmd)
  if rc != 0:
    msg = (err or out or "").strip().splitlines()
    return [], (msg[-1] if msg else f"exit {rc}")
  text = (out or "").strip()
  if not text:
    return [], None
  try:
    data = json.loads(text)
  except json.JSONDecodeError:
    return [], "non-JSON output"
  if isinstance(data, dict):
    return [data], None
  if isinstance(data, list):
    return [d for d in data if isinstance(d, dict)], None
  return [], None


def fetch_people(
  *,
  profile: str | None = None,
  include_me: bool = True,
  include_contacts: bool = True,
  queries: tuple[str, ...] | list[str] = (),
  finds: tuple[str, ...] | list[str] = (),
  limit: int = 50,
  runner: Runner = _default_runner,
) -> tuple[list[dict], list[str]]:
  """Gather people from owa-people across the requested surfaces.

  Each surface is independent: a failure on one (e.g. a 403 on `contacts`
  where Contacts.Read is not granted) is recorded as a warning and the rest
  still run. Returns (people, warnings).
  """

  def base() -> list[str]:
    cmd = ["owa-people"]
    if profile:
      cmd += ["--profile", profile]
    return cmd

  people: list[dict] = []
  warnings: list[str] = []

  def collect(label: str, cmd: list[str]) -> None:
    records, error = _invoke(cmd, runner)
    if error is not None:
      warnings.append(f"{label}: {error}")
    else:
      people.extend(records)

  if include_me:
    collect("me", base() + ["me"])
  if include_contacts:
    collect("contacts", base() + ["contacts", "--all", "--limit", str(limit)])
  for query in queries:
    collect(f"directory {query!r}", base() + ["directory", query, "--all", "--limit", str(limit)])
  for query in finds:
    # /me/people does not page, so no --all here.
    collect(f"find {query!r}", base() + ["find", query, "--limit", str(limit)])

  return people, warnings
