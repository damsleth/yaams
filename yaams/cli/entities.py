from __future__ import annotations

import re
import sys
import time

import click

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option
from yaams.config import get_db_path, load_config
from yaams.contacts_import import contacts_to_entries, fetch_contacts
from yaams.conventions import (
  EXIT_USER_ERROR,
  action_envelope,
  data_error,
  emit_action,
  emit_data_error,
)
from yaams.db import open_db
from yaams.enrich.entities import NOISE_WORDS as _NOISE_WORDS
from yaams.entities_store import save_dictionary
from yaams.people_import import (
  fetch_people,
  merge_into_dictionary,
  people_to_entries,
)
from yaams.schema import init_schema
from yaams.store import (
  add_entity_tags,
  backfill_entity_sources,
  get_entity_meta,
  get_entity_tags,
  merge_entities,
  normalize_entities,
  prune_entity,
  remove_entity_meta,
  remove_entity_tags,
  resolve_entity_id,
  seed_entities,
  set_entity_meta,
  vacuum_orphan_entities,
)


def _junk_reasons(name: str) -> list[str]:
  """Heuristic signals that an entity is an NER false positive (a common
  word, fragment, or symbol soup) rather than a real entity. Empty list means
  nothing looks off. Reasons are advisory — the caller surfaces them for human
  review, never auto-prunes."""
  stripped = name.strip()
  if not stripped:
    return ["empty"]
  reasons: list[str] = []
  nonspace = stripped.replace(" ", "")
  letters = sum(1 for c in stripped if c.isalpha())

  if stripped.casefold() in _NOISE_WORDS:
    reasons.append("stopword")
  if any(c.isalpha() for c in stripped) and stripped == stripped.lower():
    # NER capitalizes real proper nouns; an all-lowercase canonical is almost
    # always a common word or sentence fragment.
    reasons.append("all-lowercase")
  if len(stripped) <= 2 and not stripped.isupper():
    # keep short acronyms (EU, FN) which are uppercase
    reasons.append("very-short")
  if stripped.isdigit():
    reasons.append("numeric")
  if nonspace and letters / len(nonspace) < 0.5:
    reasons.append("symbol-heavy")
  return reasons


def _build_prune_candidates(conn, *, max_items: int | None = None) -> list[dict]:
  """Return NER entities that look like junk, with reasons and item counts.
  Curated (pending_review=0) and already-denied (2) entities are excluded.
  Sorted least-used first (safest to prune)."""
  rows = conn.execute(
    """
    SELECT e.canonical_name AS name, COUNT(ie.item_id) AS cnt
    FROM entities e
    LEFT JOIN item_entities ie ON ie.entity_id = e.id
    WHERE e.pending_review = 1
    GROUP BY e.id
    """
  ).fetchall()
  out: list[dict] = []
  for row in rows:
    name, cnt = row["name"], int(row["cnt"])
    if max_items is not None and cnt > max_items:
      continue
    reasons = _junk_reasons(name)
    if reasons:
      out.append({"name": name, "items": cnt, "reasons": reasons})
  out.sort(key=lambda c: (c["items"], -len(c["reasons"]), c["name"].lower()))
  return out


def _reject_interactive_json(command: str, alt_hint: str) -> None:
  click.echo(
    f"{command} is an interactive command; --json is rejected. {alt_hint}",
    err=True,
  )
  sys.exit(EXIT_USER_ERROR)


def _save_entities(config_path: str | None, entities_cfg: dict) -> None:
  """Persist the entity dictionary to the JSON store next to the database.

  The dictionary used to be spliced back into config.yaml as a YAML block,
  which appended duplicate ``entities:`` blocks over time. It now lives in its
  own JSON file (see yaams/entities_store.py); ``spacy_model`` and other knobs
  stay in config.yaml and are not touched here.
  """
  cfg = load_config(config_path)
  save_dictionary(cfg, list(entities_cfg.get("dictionary") or []))


def _add_or_update_dictionary_entry(
  dictionary: list[dict],
  canonical: str,
  etype: str,
  aliases: list[str],
) -> tuple[list[dict], bool]:
  out = [dict(entry) for entry in dictionary]
  key = canonical.lower()
  for entry in out:
    if str(entry.get("canonical", "")).lower() != key:
      continue
    entry.setdefault("type", etype)
    entry["aliases"] = _dedupe_ci(
      list(entry.get("aliases") or []) + aliases,
      exclude={canonical.casefold()},
    )
    if not entry["aliases"]:
      entry.pop("aliases", None)
    return out, False

  entry: dict = {"canonical": canonical, "type": etype}
  cleaned_aliases = _dedupe_ci(aliases, exclude={canonical.casefold()})
  if cleaned_aliases:
    entry["aliases"] = cleaned_aliases
  out.append(entry)
  return out, True


@cli.group("entities")
def entities_group() -> None:
  """Manage the entity dictionary used for promotion candidate clustering."""
  pass


@entities_group.command("list")
@config_option
@click.option("--json", "as_json", is_flag=True, help="Raw entities document on stdout.")
def entities_list(config_path: str, as_json: bool) -> None:
  """Show all dictionary entities with item hit counts."""
  cfg = load_config(config_path)
  dictionary = (cfg.get("entities") or {}).get("dictionary") or []
  if not dictionary:
    if as_json:
      import json as _json
      # Data-class success: no top-level `ok`.
      click.echo(_json.dumps({"entities": []}, ensure_ascii=False))
      return
    click.echo("No entities in dictionary. Add some with: entities add <name>")
    return
  db_path = get_db_path(cfg)
  try:
    conn = open_db(db_path, readonly=True)
  except Exception as exc:
    if as_json:
      emit_data_error(data_error(
        command="entities list", code="db_open_failed", message=str(exc),
        hint="Run: yaams init-db",
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
  out: list[dict] = []
  try:
    for entry in dictionary:
      canonical = entry["canonical"]
      etype = entry.get("type", "?")
      aliases = entry.get("aliases") or []
      row = conn.execute(
        """SELECT count(*) FROM item_entities ie
           JOIN entities e ON e.id = ie.entity_id
           WHERE e.canonical_name = ? AND ie.source = 'dictionary'""",
        (canonical,),
      ).fetchone()
      count = row[0] if row else 0
      if as_json:
        out.append({
          "canonical": canonical,
          "type": etype,
          "aliases": list(aliases),
          "items": int(count),
        })
      else:
        alias_str = f"  aliases: {', '.join(aliases)}" if aliases else ""
        click.echo(f"  {canonical:<22} {etype:<12} {count:>5} items{alias_str}")
  finally:
    conn.close()
  if as_json:
    import json as _json
    click.echo(_json.dumps({"entities": out}, ensure_ascii=False))


@entities_group.command("add")
@click.argument("canonical")
@click.option("--type", "etype", default="person", show_default=True)
@click.option("--alias", "aliases", multiple=True, help="Repeatable: --alias EP --alias Ex.Person")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_add(
  canonical: str,
  etype: str,
  aliases: tuple[str, ...],
  as_json: bool,
  config_path: str,
) -> None:
  """Add an entity to the dictionary and seed the DB immediately."""
  t0 = time.monotonic()
  cfg = load_config(config_path)
  entities_cfg = dict(cfg.get("entities") or {})
  dictionary = list(entities_cfg.get("dictionary") or [])
  if any(e["canonical"].lower() == canonical.lower() for e in dictionary):
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="entities add", ok=True,
        stats={"canonical": canonical, "added": False, "reason": "already_present"},
        duration_ms=duration_ms,
      ))
      return
    click.echo(f"'{canonical}' is already in the dictionary.")
    return
  entry: dict = {"canonical": canonical, "type": etype}
  if aliases:
    entry["aliases"] = list(aliases)
  dictionary.append(entry)
  entities_cfg["dictionary"] = dictionary
  _save_entities(config_path, entities_cfg)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    dictionary = load_config(config_path).get("entities", {}).get("dictionary", [])
    seed_entities(conn, dictionary)
    backfill_entity_sources(conn, dictionary)
  finally:
    conn.close()
  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="entities add", ok=True,
      stats={"canonical": canonical, "type": etype, "aliases": list(aliases), "added": True},
      duration_ms=duration_ms,
    ))
    return
  click.echo(f"Added '{canonical}' ({etype}).")


@entities_group.command("import-people")
@config_option
@click.option("--profile", default=None, help="owa-piggy profile (default: owa-people's configured default).")
@click.option("--query", "queries", multiple=True, help="Directory search term to pull colleagues (repeatable).")
@click.option("--find", "finds", multiple=True, help="/me/people relevance search term (repeatable).")
@click.option("--contacts/--no-contacts", "contacts", default=True, show_default=True,
              help="Include personal contacts (skipped with a warning if the scope is denied).")
@click.option("--me/--no-me", "include_me", default=True, show_default=True,
              help="Include the authenticated user.")
@click.option("--limit", default=50, show_default=True, type=int, help="Per-query page size.")
@click.option("--type", "etype", default="person", show_default=True, help="Entity type for imported people.")
@click.option("--tag", "tags", multiple=True, help="Tag attached to every imported entity (repeatable).")
@click.option("--dry-run", is_flag=True, help="Preview entries without writing config or seeding the DB.")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def entities_import_people(
  config_path: str,
  profile: str | None,
  queries: tuple[str, ...],
  finds: tuple[str, ...],
  contacts: bool,
  include_me: bool,
  limit: int,
  etype: str,
  tags: tuple[str, ...],
  dry_run: bool,
  as_json: bool,
) -> None:
  """Import M365 people (via owa-people) into the entity dictionary.

  Pulls the authenticated user, personal contacts, and any --query/--find
  search results, maps each person to a {canonical, type, aliases: [email]}
  dictionary entry, and seeds them so NER/tagging resolves colleagues across
  every source. Existing entries gain new email aliases; nothing is removed.
  Each owa-people surface is independent; a denied scope (e.g. contacts)
  becomes a warning, not a failure, as long as another surface returns people.
  """
  t0 = time.monotonic()
  people, warnings = fetch_people(
    profile=profile,
    include_me=include_me,
    include_contacts=contacts,
    queries=queries,
    finds=finds,
    limit=limit,
  )
  entries = people_to_entries(people, etype=etype)
  fetched = len(people)

  # Every attempted surface failed and we have nothing to import: surface it
  # as an error so automation notices, mirroring ingest's all-sources-failed.
  if fetched == 0 and warnings:
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="entities import-people", ok=False,
        error={"code": "all_sources_failed",
               "message": "owa-people returned no people from any surface"},
        warnings=warnings, duration_ms=duration_ms,
      ))
      sys.exit(EXIT_USER_ERROR)
    click.echo("No people imported - every owa-people surface failed:", err=True)
    for w in warnings:
      click.echo(f"  - {w}", err=True)
    sys.exit(EXIT_USER_ERROR)

  cfg = load_config(config_path)
  entities_cfg = dict(cfg.get("entities") or {})
  dictionary = list(entities_cfg.get("dictionary") or [])
  merged, stats = merge_into_dictionary(dictionary, entries)
  changed = bool(stats["added"] or stats["updated"])

  if dry_run:
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="entities import-people", ok=True,
        stats={"fetched": fetched, "entries": len(entries), "dry_run": True, **stats},
        warnings=warnings, duration_ms=duration_ms,
      ))
      return
    click.echo(f"[dry-run] {fetched} people fetched, {len(entries)} unique; "
               f"would add {stats['added']}, update {stats['updated']} "
               f"(+{stats['aliases_added']} aliases).")
    for w in warnings:
      click.echo(f"  warning: {w}", err=True)
    return

  if changed:
    entities_cfg["dictionary"] = merged
    _save_entities(config_path, entities_cfg)

  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  applied_tags = 0
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    fresh = load_config(config_path).get("entities", {}).get("dictionary", [])
    seed_entities(conn, fresh)
    backfill_entity_sources(conn, fresh)
    if tags:
      for entry in entries:
        eid = resolve_entity_id(conn, entry["canonical"])
        if eid is not None:
          applied_tags += add_entity_tags(conn, eid, tags)
  finally:
    conn.close()

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="entities import-people", ok=True,
      stats={"fetched": fetched, "entries": len(entries),
             "tags_added": applied_tags, **stats},
      warnings=warnings, duration_ms=duration_ms,
    ))
    return
  click.echo(f"Imported {fetched} people -> {stats['added']} added, "
             f"{stats['updated']} updated (+{stats['aliases_added']} aliases).")


@entities_group.command("import-contacts")
@config_option
@click.option("--type", "etype", default="person", show_default=True,
              help="Entity type for imported people.")
@click.option("--org-type", default="org", show_default=True,
              help="Entity type for company cards (no person name, only an organization).")
@click.option("--default-cc", default="+47", show_default=True,
              help="Country code applied to bare national numbers of that country's length.")
@click.option("--tag", "tags", multiple=True, help="Tag attached to every imported entity (repeatable).")
@click.option("--dry-run", is_flag=True, help="Preview entries without writing config or seeding the DB.")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def entities_import_contacts(
  config_path: str,
  etype: str,
  org_type: str,
  default_cc: str,
  tags: tuple[str, ...],
  dry_run: bool,
  as_json: bool,
) -> None:
  """Import the macOS address book into the entity dictionary.

  Reads every AddressBook store read-only, maps each card to a
  {canonical, type, aliases} entry whose aliases are E.164 phone numbers and
  lowercased emails, and seeds them so the tagger resolves iMessage senders
  instead of leaving them as bare numbers. Existing entries gain new aliases;
  nothing is removed. An identifier claimed by two different cards is left with
  whoever holds it and reported as a warning, never silently reassigned.
  """
  t0 = time.monotonic()
  contacts, warnings = fetch_contacts()
  entries, collisions = contacts_to_entries(
    contacts, etype=etype, org_type=org_type, default_cc=default_cc
  )
  warnings = [*warnings, *collisions]
  fetched = len(contacts)

  if fetched == 0:
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="entities import-contacts", ok=False,
        error={"code": "no_contacts",
               "message": "no readable AddressBook store returned any contact"},
        warnings=warnings, duration_ms=duration_ms,
      ))
      sys.exit(EXIT_USER_ERROR)
    click.echo("No contacts imported - no readable AddressBook store:", err=True)
    for w in warnings:
      click.echo(f"  - {w}", err=True)
    sys.exit(EXIT_USER_ERROR)

  cfg = load_config(config_path)
  entities_cfg = dict(cfg.get("entities") or {})
  dictionary = list(entities_cfg.get("dictionary") or [])
  merged, stats = merge_into_dictionary(dictionary, entries)
  changed = bool(stats["added"] or stats["updated"])

  if dry_run:
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="entities import-contacts", ok=True,
        stats={"fetched": fetched, "entries": len(entries), "dry_run": True, **stats},
        warnings=warnings, duration_ms=duration_ms,
      ))
      return
    click.echo(f"[dry-run] {fetched} contacts read, {len(entries)} unique; "
               f"would add {stats['added']}, update {stats['updated']} "
               f"(+{stats['aliases_added']} aliases).")
    for w in warnings:
      click.echo(f"  warning: {w}", err=True)
    return

  if changed:
    entities_cfg["dictionary"] = merged
    _save_entities(config_path, entities_cfg)

  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  applied_tags = 0
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    fresh = load_config(config_path).get("entities", {}).get("dictionary", [])
    seed_entities(conn, fresh)
    backfill_entity_sources(conn, fresh)
    if tags:
      for entry in entries:
        eid = resolve_entity_id(conn, entry["canonical"])
        if eid is not None:
          applied_tags += add_entity_tags(conn, eid, tags)
  finally:
    conn.close()

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="entities import-contacts", ok=True,
      stats={"fetched": fetched, "entries": len(entries),
             "tags_added": applied_tags, **stats},
      warnings=warnings, duration_ms=duration_ms,
    ))
    return
  click.echo(f"Imported {fetched} contacts -> {stats['added']} added, "
             f"{stats['updated']} updated (+{stats['aliases_added']} aliases).")
  for w in warnings:
    click.echo(f"  warning: {w}", err=True)
  if applied_tags:
    click.echo(f"  tagged +{applied_tags}: {', '.join(t.lower() for t in tags)}")
  for w in warnings:
    click.echo(f"  warning: {w}", err=True)
  if changed:
    click.echo("  Next: 'yaams enrich retag' to relabel historical items with the new entities.")


@entities_group.command("remove")
@click.argument("canonical")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_remove(canonical: str, as_json: bool, config_path: str) -> None:
  """Remove an entity from the dictionary (existing DB links are kept)."""
  t0 = time.monotonic()
  cfg = load_config(config_path)
  entities_cfg = dict(cfg.get("entities") or {})
  dictionary = list(entities_cfg.get("dictionary") or [])
  before = len(dictionary)
  dictionary = [e for e in dictionary if e["canonical"].lower() != canonical.lower()]
  if len(dictionary) == before:
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="entities remove", ok=True,
        stats={"canonical": canonical, "removed": False, "reason": "not_found"},
        duration_ms=duration_ms,
      ))
      return
    click.echo(f"'{canonical}' not found in dictionary.")
    return
  entities_cfg["dictionary"] = dictionary
  _save_entities(config_path, entities_cfg)
  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="entities remove", ok=True,
      stats={"canonical": canonical, "removed": True, "remaining": len(dictionary)},
      warnings=["Existing DB links to this entity are kept; entity-source rows survive."],
      duration_ms=duration_ms,
    ))
    return
  click.echo(f"Removed '{canonical}'. Existing item links in the DB are untouched.")


@entities_group.command("discover")
@config_option
@click.option("--min-count", default=5, show_default=True, help="Minimum appearances to surface a candidate")
@click.option("--limit", default=50, show_default=True, help="Max candidates to review")
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="(Rejected - discover is interactive; use 'entities list --json' for current entities.)",
)
def entities_discover(config_path: str, min_count: int, limit: int, as_json: bool) -> None:
  """Scan NER-tagged items and suggest new dictionary entries interactively."""
  if as_json:
    _reject_interactive_json(
      "entities discover",
      "Use `yaams entities list --json` for current dictionary state.",
    )
  cfg = load_config(config_path)
  known = {e["canonical"].lower() for e in (cfg.get("entities") or {}).get("dictionary") or []}

  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))

    rows = conn.execute(
      """
      SELECT e.id, e.canonical_name, e.entity_type, count(*) AS cnt
      FROM item_entities ie
      JOIN entities e ON e.id = ie.entity_id
      WHERE ie.source = 'ner'
        AND e.pending_review != 2
      GROUP BY e.id
      HAVING cnt >= ?
      ORDER BY cnt DESC
      LIMIT ?
      """,
      (min_count, limit * 3),
    ).fetchall()

    candidates = [
      r for r in rows
      if r["canonical_name"].lower() not in known
      and r["canonical_name"].lower() not in _NOISE_WORDS
      and not r["canonical_name"].islower()
      and len(r["canonical_name"]) > 2
      and not r["canonical_name"].isdigit()
    ][:limit]

    if not candidates:
      click.echo(f"No NER candidates with {min_count}+ appearances not already in dictionary.")
      return

    click.echo(f"Found {len(candidates)} candidates.  [a]ccept  [e]dit  [d]eny  [q]uit\n")

    for i, row in enumerate(candidates, 1):
      original_id = row["id"]
      canonical = row["canonical_name"]
      etype = row["entity_type"]
      cnt = row["cnt"]

      samples = conn.execute(
        """
        SELECT i.content, i.source, i.timestamp
        FROM item_entities ie
        JOIN entities e ON e.id = ie.entity_id
        JOIN items i ON i.id = ie.item_id
        WHERE e.id = ? AND ie.source = 'ner'
        ORDER BY i.timestamp DESC
        LIMIT 2
        """,
        (original_id,),
      ).fetchall()

      click.echo(f"[{i}/{len(candidates)}] {canonical!r}  type={etype}  appearances={cnt}")
      for s in samples:
        snippet = (s["content"] or "")[:120].replace("\n", " ")
        click.echo(f"  [{s['source']} {(s['timestamp'] or '')[:10]}] {snippet}")

      while True:
        choice = click.prompt("", default="d", prompt_suffix="[a/e/d/q] > ").strip().lower()

        if choice == "q":
          click.echo("Done.")
          return

        if choice == "d":
          with conn:
            conn.execute(
              """
              UPDATE entities SET pending_review = 2
              WHERE id = ?
              """,
              (original_id,),
            )
          break

        if choice in ("a", "e"):
          new_canonical = canonical
          new_type = etype
          aliases: list[str] = []
          if choice == "e":
            new_canonical = click.prompt("  Canonical name", default=canonical).strip()
            new_type = click.prompt("  Type", default=etype).strip()
            raw = click.prompt("  Aliases (comma-separated, or blank)", default="").strip()
            aliases = [a.strip() for a in raw.split(",") if a.strip()]

          entities_cfg = dict(cfg.get("entities") or {})
          dictionary = list(entities_cfg.get("dictionary") or [])
          merge_aliases = list(aliases)
          if canonical.lower() != new_canonical.lower():
            merge_aliases.append(canonical)
          dictionary, added = _add_or_update_dictionary_entry(
            dictionary, new_canonical, new_type, merge_aliases
          )
          entities_cfg["dictionary"] = dictionary
          _save_entities(config_path, entities_cfg)
          cfg = load_config(config_path)
          known.add(new_canonical.lower())
          d = cfg.get("entities", {}).get("dictionary", [])
          seed_entities(conn, d)
          target_id = resolve_entity_id(conn, new_canonical)
          if target_id is not None and target_id != original_id:
            merge_entities(conn, target_id, [original_id])
          backfill_entity_sources(conn, d)
          verb = "Added" if added else "Updated"
          click.echo(f"  {verb} '{new_canonical}'.")
          break

        click.echo("  Use a, e, d, or q.")

      click.echo()

    click.echo("Review complete.")
  finally:
    conn.close()


@entities_group.command("denied")
@config_option
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="(Rejected - denied is interactive; use 'entities list --json' for current entities.)",
)
def entities_denied(config_path: str, as_json: bool) -> None:
  """List previously denied NER candidates and optionally restore them."""
  if as_json:
    _reject_interactive_json(
      "entities denied",
      "Use `yaams entities list --json` for current dictionary state.",
    )
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    rows = conn.execute(
      """
      SELECT e.id, e.canonical_name, e.entity_type, count(ie.item_id) AS cnt
      FROM entities e
      LEFT JOIN item_entities ie ON ie.entity_id = e.id
      WHERE e.pending_review = 2
      GROUP BY e.id
      ORDER BY cnt DESC
      """,
    ).fetchall()
    if not rows:
      click.echo("No denied entities.")
      return
    click.echo(f"{len(rows)} denied entities.  [u]ndeny  [q]uit\n")
    for row in rows:
      click.echo(f"  {row['canonical_name']:<28} {row['entity_type']:<12} {row['cnt']} appearances")
      choice = click.prompt("", default="q", prompt_suffix="[u/q] > ").strip().lower()
      if choice == "u":
        with conn:
          conn.execute(
            "UPDATE entities SET pending_review = 1 WHERE id = ?", (row["id"],)
          )
        click.echo(f"  '{row['canonical_name']}' restored - will appear in discover again.")
      elif choice == "q":
        return
    click.echo("Done.")
  finally:
    conn.close()


@entities_group.command("manage")
@config_option
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="(Rejected - entities manage is interactive; use 'entities list --json' for machine output.)",
)
def entities_manage(config_path: str, as_json: bool) -> None:
  """Interactive entity dictionary manager."""
  if as_json:
    import sys
    click.echo(
      "entities manage is an interactive command; --json is rejected. "
      "Use `yaams entities list --json` for machine-readable entity data.",
      err=True,
    )
    sys.exit(1)


  def _show(cfg: dict, conn) -> None:
    dictionary = (cfg.get("entities") or {}).get("dictionary") or []
    if not dictionary:
      click.echo("  (empty - add some with [a])")
      return
    for entry in dictionary:
      canonical = entry["canonical"]
      etype = entry.get("type", "?")
      aliases = entry.get("aliases") or []
      row = conn.execute(
        """SELECT count(*) FROM item_entities ie
           JOIN entities e ON e.id = ie.entity_id
           WHERE e.canonical_name = ? AND ie.source = 'dictionary'""",
        (canonical,),
      ).fetchone()
      count = row[0] if row else 0
      alias_str = f" [{', '.join(aliases)}]" if aliases else ""
      click.echo(f"  {canonical:<22} {etype:<12} {count:>5} items{alias_str}")

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    while True:
      cfg = load_config(config_path)
      click.echo("\nEntity dictionary:")
      _show(cfg, conn)
      click.echo("\n  [a]dd  [r]emove  [q]uit")
      choice = click.prompt("", prompt_suffix="> ", default="q").strip().lower()

      if choice == "q":
        break

      elif choice == "a":
        canonical = click.prompt("  Name").strip()
        if not canonical:
          continue
        etype = click.prompt("  Type", default="person").strip()
        raw = click.prompt("  Aliases (comma-separated, or blank)", default="").strip()
        aliases = [a.strip() for a in raw.split(",") if a.strip()]
        entities_cfg = dict(cfg.get("entities") or {})
        dictionary = list(entities_cfg.get("dictionary") or [])
        if any(e["canonical"].lower() == canonical.lower() for e in dictionary):
          click.echo(f"  '{canonical}' already exists.")
          continue
        entry: dict = {"canonical": canonical, "type": etype}
        if aliases:
          entry["aliases"] = aliases
        dictionary.append(entry)
        entities_cfg["dictionary"] = dictionary
        _save_entities(config_path, entities_cfg)
        d = load_config(config_path).get("entities", {}).get("dictionary", [])
        seed_entities(conn, d)
        backfill_entity_sources(conn, d)
        click.echo(f"  Added '{canonical}'.")

      elif choice == "r":
        canonical = click.prompt("  Remove which entity?").strip()
        entities_cfg = dict(cfg.get("entities") or {})
        dictionary = list(entities_cfg.get("dictionary") or [])
        before = len(dictionary)
        dictionary = [e for e in dictionary if e["canonical"].lower() != canonical.lower()]
        if len(dictionary) == before:
          click.echo(f"  '{canonical}' not found.")
          continue
        entities_cfg["dictionary"] = dictionary
        _save_entities(config_path, entities_cfg)
        click.echo(f"  Removed '{canonical}'.")

  finally:
    conn.close()


def _open_for_entity(config_path: str, command: str, name: str, as_json: bool, *, readonly: bool):
  """Open the DB and resolve an entity by canonical name, emitting the proper
  envelope and exiting on failure. Returns (conn, entity_id)."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=readonly)
  if not readonly:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
  eid = resolve_entity_id(conn, name)
  if eid is None:
    conn.close()
    msg = f"No entity named {name!r}"
    if as_json:
      emit_data_error(data_error(command=command, code="unknown_entity", message=msg,
                                 hint="List entities with: yaams entities list"))
    else:
      click.echo(msg + ".", err=True)
    sys.exit(EXIT_USER_ERROR)
  return conn, eid


def _parse_kv(pairs: tuple[str, ...], command: str, as_json: bool) -> list[tuple[str, str]]:
  out: list[tuple[str, str]] = []
  for raw in pairs:
    if "=" not in raw:
      msg = f"Expected KEY=VALUE, got {raw!r}"
      if as_json:
        emit_data_error(data_error(command=command, code="bad_attribute", message=msg,
                                   hint="Use: yaams entities set <name> sector=public"))
      else:
        click.echo(msg + ".", err=True)
      sys.exit(EXIT_USER_ERROR)
    key, value = raw.split("=", 1)
    if not key.strip():
      msg = f"Empty attribute key in {raw!r}"
      if as_json:
        emit_data_error(data_error(command=command, code="bad_attribute", message=msg))
      else:
        click.echo(msg + ".", err=True)
      sys.exit(EXIT_USER_ERROR)
    out.append((key, value))
  return out


@entities_group.command("tag")
@click.argument("name")
@click.argument("tags", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_tag(name: str, tags: tuple[str, ...], as_json: bool, config_path: str) -> None:
  """Attach membership TAGS to an entity (e.g. customer defense-sector)."""
  conn, eid = _open_for_entity(config_path, "entities tag", name, as_json, readonly=False)
  try:
    added = add_entity_tags(conn, eid, tags)
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities tag", ok=True,
                                stats={"entity": name, "added": added}))
    return
  click.echo(f"Tagged '{name}' (+{added} new): {', '.join(t.lower() for t in tags)}")


@entities_group.command("untag")
@click.argument("name")
@click.argument("tags", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_untag(name: str, tags: tuple[str, ...], as_json: bool, config_path: str) -> None:
  """Remove membership TAGS from an entity."""
  conn, eid = _open_for_entity(config_path, "entities untag", name, as_json, readonly=False)
  try:
    removed = remove_entity_tags(conn, eid, tags)
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities untag", ok=True,
                                stats={"entity": name, "removed": removed}))
    return
  click.echo(f"Untagged '{name}' (-{removed}).")


def _dictionary_entry(dictionary: list[dict], canonical: str) -> dict | None:
  """Find a dictionary entry by canonical name, case-insensitively."""
  target = canonical.casefold()
  for entry in dictionary:
    if str(entry.get("canonical", "")).casefold() == target:
      return entry
  return None


@entities_group.command("rename")
@click.argument("old")
@click.argument("new")
@click.option("--drop-old-alias", is_flag=True,
              help="Do not keep OLD as an alias. Only for a typo fix, where nothing "
                   "in the corpus actually uses the old spelling.")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_rename(old: str, new: str, drop_old_alias: bool, as_json: bool,
                    config_path: str) -> None:
  """Rename an entity's canonical name, keeping OLD as an alias.

  Renaming in place rather than merging keeps the entity row, so every item
  link, tag, meta value and relation follows automatically. OLD is kept as an
  alias by default because the corpus still says it: dropping it would stop
  historical mentions resolving. Use --drop-old-alias only for a typo.

  If NEW already names a different entity this refuses and points at `merge`,
  which is the operation that folds two entities together.
  """
  new = new.strip()
  if not new:
    click.echo("New name cannot be empty.", err=True)
    sys.exit(EXIT_USER_ERROR)
  conn, eid = _open_for_entity(config_path, "entities rename", old, as_json, readonly=False)
  try:
    canonical, etype, aliases = _entity_row(conn, eid)
    clash = resolve_entity_id(conn, new)
    if clash is not None and clash != eid:
      conn.close()
      msg = f"'{new}' already names a different entity"
      if as_json:
        emit_data_error(data_error(
          command="entities rename", code="name_taken", message=msg,
          hint=f"To combine them: yaams entities merge {new!r} {canonical!r}"))
      else:
        click.echo(msg + f". To combine them: yaams entities merge {new!r} {canonical!r}", err=True)
      sys.exit(EXIT_USER_ERROR)

    kept = list(aliases) if drop_old_alias else [canonical, *aliases]
    kept = _dedupe_ci(kept, exclude={new.casefold()})

    cfg = load_config(config_path)
    entities_cfg = dict(cfg.get("entities") or {})
    dictionary = list(entities_cfg.get("dictionary") or [])
    entry = _dictionary_entry(dictionary, canonical)
    if entry is None:
      entry = {"canonical": new, "type": etype}
      dictionary.append(entry)
    entry["canonical"] = new
    if kept:
      entry["aliases"] = kept
    else:
      entry.pop("aliases", None)
    entities_cfg["dictionary"] = dictionary
    _save_entities(config_path, entities_cfg)

    # Rename the row itself before reseeding: seed_entities matches on
    # canonical_name, so reseeding first would create a second entity and
    # strand every existing link on the old one.
    with conn:
      conn.execute("UPDATE entities SET canonical_name = ? WHERE id = ?", (new, eid))
    fresh = load_config(config_path).get("entities", {}).get("dictionary", [])
    seed_entities(conn, fresh)
  finally:
    conn.close()

  if as_json:
    emit_action(action_envelope(command="entities rename", ok=True, stats={
      "old": canonical, "new": new, "aliases": kept, "old_kept_as_alias": not drop_old_alias,
    }))
    return
  click.echo(f"Renamed '{canonical}' -> '{new}'.")
  click.echo(f"  aliases now: {', '.join(kept) if kept else '-'}")


@entities_group.command("unalias")
@click.argument("name")
@click.argument("aliases", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_unalias(name: str, aliases: tuple[str, ...], as_json: bool,
                     config_path: str) -> None:
  """Remove ALIASES from an entity, leaving the entity itself in place.

  `remove` deletes a whole entity, which is the wrong tool when one alias is
  simply wrong: a shared phone number folded onto the wrong card, or a
  descriptive card name ("Cecilie Emilie Sin Venn") that should never have
  become a way of referring to someone. Matching is case-insensitive, and an
  alias that is not present is reported rather than silently ignored.
  """
  conn, eid = _open_for_entity(config_path, "entities unalias", name, as_json, readonly=False)
  try:
    canonical, etype, current = _entity_row(conn, eid)
    drop = {a.strip().casefold() for a in aliases if a.strip()}
    cfg = load_config(config_path)
    entities_cfg = dict(cfg.get("entities") or {})
    dictionary = list(entities_cfg.get("dictionary") or [])
    entry = _dictionary_entry(dictionary, canonical)
    source = list(entry.get("aliases") or []) if entry else list(current)
    kept = [a for a in source if a.strip().casefold() not in drop]
    removed = [a for a in source if a.strip().casefold() in drop]
    missing = sorted(drop - {a.strip().casefold() for a in removed})

    if entry is None:
      entry = {"canonical": canonical, "type": etype}
      dictionary.append(entry)
    if kept:
      entry["aliases"] = kept
    else:
      entry.pop("aliases", None)
    entities_cfg["dictionary"] = dictionary
    _save_entities(config_path, entities_cfg)
    # seed_entities rewrites the aliases column wholesale, so a reseed is what
    # actually drops them from the DB.
    fresh = load_config(config_path).get("entities", {}).get("dictionary", [])
    seed_entities(conn, fresh)
  finally:
    conn.close()

  if as_json:
    emit_action(action_envelope(command="entities unalias", ok=True, stats={
      "entity": canonical, "removed": removed, "not_found": missing, "aliases": kept,
    }))
    return
  click.echo(f"Unaliased '{canonical}' (-{len(removed)}).")
  if missing:
    click.echo(f"  not an alias: {', '.join(missing)}", err=True)
  click.echo(f"  aliases now: {', '.join(kept) if kept else '-'}")


@entities_group.command("set")
@click.argument("name")
@click.argument("attrs", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_set(name: str, attrs: tuple[str, ...], as_json: bool, config_path: str) -> None:
  """Set KEY=VALUE attribute(s) on an entity (e.g. sector=public region=oslo)."""
  pairs = _parse_kv(attrs, "entities set", as_json)
  conn, eid = _open_for_entity(config_path, "entities set", name, as_json, readonly=False)
  try:
    for key, value in pairs:
      set_entity_meta(conn, eid, key, value)
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities set", ok=True,
                                stats={"entity": name, "set": len(pairs)}))
    return
  click.echo(f"Set on '{name}': " + ", ".join(f"{k.lower()}={v}" for k, v in pairs))


@entities_group.command("unset")
@click.argument("name")
@click.argument("keys", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_unset(name: str, keys: tuple[str, ...], as_json: bool, config_path: str) -> None:
  """Remove attribute KEY(s) from an entity."""
  conn, eid = _open_for_entity(config_path, "entities unset", name, as_json, readonly=False)
  try:
    removed = remove_entity_meta(conn, eid, keys)
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities unset", ok=True,
                                stats={"entity": name, "removed": removed}))
    return
  click.echo(f"Unset on '{name}' (-{removed}).")


@entities_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Raw entity document on stdout.")
@config_option
def entities_show(name: str, as_json: bool, config_path: str) -> None:
  """Show an entity's type, aliases, tags, attributes, and item count."""
  import json as _json

  conn, eid = _open_for_entity(config_path, "entities show", name, as_json, readonly=True)
  try:
    row = conn.execute(
      "SELECT canonical_name, entity_type, aliases FROM entities WHERE id = ?", (eid,)
    ).fetchone()
    canonical = row["canonical_name"]
    etype = row["entity_type"]
    try:
      aliases = _json.loads(row["aliases"]) if row["aliases"] else []
    except (TypeError, ValueError):
      aliases = []
    tags = get_entity_tags(conn, eid)
    meta = get_entity_meta(conn, eid)
    items = conn.execute(
      "SELECT count(*) FROM item_entities WHERE entity_id = ?", (eid,)
    ).fetchone()[0]
  finally:
    conn.close()
  if as_json:
    click.echo(_json.dumps({
      "canonical": canonical, "type": etype, "aliases": aliases,
      "tags": tags, "meta": meta, "items": int(items),
    }, ensure_ascii=False))
    return
  click.echo(f"{canonical}  ({etype})")
  click.echo(f"  items:   {items}")
  click.echo(f"  aliases: {', '.join(aliases) if aliases else '-'}")
  click.echo(f"  tags:    {', '.join(tags) if tags else '-'}")
  if meta:
    click.echo("  meta:")
    for k, v in meta.items():
      click.echo(f"    {k} = {v}")
  else:
    click.echo("  meta:    -")


def _entity_row(conn, eid: int):
  """Return (canonical_name, entity_type, aliases_list) for an entity id."""
  import json as _json
  row = conn.execute(
    "SELECT canonical_name, entity_type, aliases FROM entities WHERE id = ?", (eid,)
  ).fetchone()
  try:
    aliases = _json.loads(row["aliases"]) if row["aliases"] else []
  except (TypeError, ValueError):
    aliases = []
  return row["canonical_name"], row["entity_type"], [a for a in aliases if isinstance(a, str)]


def _dedupe_ci(values, *, exclude: set[str] | None = None) -> list[str]:
  """Case-insensitive de-dupe preserving first-seen order, dropping any value
  whose casefold is in `exclude`."""
  seen = set(exclude or set())
  out: list[str] = []
  for v in values:
    if not v:
      continue
    key = v.casefold()
    if key not in seen:
      seen.add(key)
      out.append(v)
  return out


def _apply_merge(conn, config_path: str, survivor_id: int, victim_ids: list[int]):
  """Fold victim names+aliases into the survivor's config-dictionary entry,
  reseed, then repoint and delete the victims. Shared by the `merge` command
  and the interactive dedupe TUI. Returns (survivor_name, stats, aliases)."""
  cfg = load_config(config_path)
  survivor_name, survivor_type, survivor_aliases = _entity_row(conn, survivor_id)
  folded = list(survivor_aliases)
  victim_names: list[str] = []
  for vid in victim_ids:
    vname, _vtype, valiases = _entity_row(conn, vid)
    victim_names.append(vname)
    folded.append(vname)
    folded.extend(valiases)
  merged_aliases = _dedupe_ci(folded, exclude={survivor_name.casefold()})

  entities_cfg = dict(cfg.get("entities") or {})
  dictionary = list(entities_cfg.get("dictionary") or [])
  victim_lc = {n.casefold() for n in victim_names}
  survivor_entry = None
  for entry in dictionary:
    if str(entry.get("canonical", "")).casefold() == survivor_name.casefold():
      survivor_entry = entry
      break
  if survivor_entry is None:
    survivor_entry = {"canonical": survivor_name, "type": survivor_type}
    dictionary.append(survivor_entry)
  survivor_entry["aliases"] = _dedupe_ci(
    list(survivor_entry.get("aliases") or []) + merged_aliases,
    exclude={survivor_name.casefold()},
  )
  dictionary = [
    e for e in dictionary if str(e.get("canonical", "")).casefold() not in victim_lc
  ]
  entities_cfg["dictionary"] = dictionary
  _save_entities(config_path, entities_cfg)

  fresh_dict = load_config(config_path).get("entities", {}).get("dictionary", [])
  seed_entities(conn, fresh_dict)
  reseeded_id = resolve_entity_id(conn, survivor_name)
  assert reseeded_id is not None
  stats = merge_entities(conn, reseeded_id, victim_ids)
  return survivor_name, stats, merged_aliases


def _build_merge_suggestions(conn, min_items: int) -> list[dict]:
  """Group entities whose names collapse to the same normalized key into
  merge candidates. Each suggestion: {key, survivor, victims, members}.
  Survivor is the member with the most item links (ties: shortest name)."""
  rows = conn.execute(
    """
    SELECT e.canonical_name AS name, COUNT(ie.item_id) AS cnt
    FROM entities e
    LEFT JOIN item_entities ie ON ie.entity_id = e.id
    WHERE e.pending_review != 2
    GROUP BY e.id
    """
  ).fetchall()

  groups: dict[str, list[tuple[str, int]]] = {}
  for row in rows:
    name, cnt = row["name"], int(row["cnt"])
    if cnt < min_items:
      continue
    key = _norm_merge_key(name)
    if not key:
      continue
    groups.setdefault(key, []).append((name, cnt))

  suggestions: list[dict] = []
  for key, members in groups.items():
    if len(members) < 2:
      continue
    members.sort(key=lambda m: (-m[1], len(m[0])))
    suggestions.append({
      "key": key,
      "survivor": members[0][0],
      "victims": [m[0] for m in members[1:]],
      "members": [{"name": n, "items": c} for n, c in members],
    })
  suggestions.sort(key=lambda s: -sum(m["items"] for m in s["members"]))
  return suggestions


@entities_group.command("merge")
@click.argument("survivor")
@click.argument("victims", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_merge(survivor: str, victims: tuple[str, ...], as_json: bool, config_path: str) -> None:
  """Merge VICTIMS into SURVIVOR: fold their names+aliases into the survivor,
  repoint all links/tags/meta/relations, and delete the victim entities.

  The victim names become survivor aliases in your config dictionary, so the
  merge is durable - future NER re-tagging resolves them to the survivor."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    survivor_id = resolve_entity_id(conn, survivor)
    missing = [] if survivor_id is not None else [survivor]
    victim_ids: list[int] = []
    for v in victims:
      vid = resolve_entity_id(conn, v)
      if vid is None:
        missing.append(v)
      elif vid != survivor_id and vid not in victim_ids:
        victim_ids.append(vid)
    if missing:
      conn.close()
      msg = f"Unknown entit{'ies' if len(missing) > 1 else 'y'}: {', '.join(missing)}"
      if as_json:
        emit_data_error(data_error(command="entities merge", code="unknown_entity",
                                   message=msg, hint="List entities with: yaams entities list"))
      else:
        click.echo(msg + ".", err=True)
      sys.exit(EXIT_USER_ERROR)
    if not victim_ids:
      conn.close()
      if as_json:
        emit_action(action_envelope(command="entities merge", ok=True,
                                    stats={"survivor": survivor, "victims": 0}))
        return
      click.echo("Nothing to merge (victims resolved to the survivor).")
      return

    survivor_name, stats, merged_aliases = _apply_merge(
      conn, config_path, survivor_id, victim_ids
    )
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities merge", ok=True, stats={
      "survivor": survivor_name, "victims": stats["victims"],
      "item_links": stats["item_links"], "aliases_added": len(merged_aliases),
    }))
    return
  click.echo(f"Merged {stats['victims']} entit{'ies' if stats['victims'] != 1 else 'y'} "
             f"into '{survivor_name}' ({stats['item_links']} item links repointed).")
  click.echo(f"  aliases now: {', '.join(merged_aliases) if merged_aliases else '-'}")
  click.echo("  Next: 'yaams assoc build' to refresh associations"
             " (and 'yaams enrich retag' to relabel historical links).")


@entities_group.command("prune")
@click.argument("names", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def entities_prune(names: tuple[str, ...], as_json: bool, config_path: str) -> None:
  """Deny junk ENTITIES: mark them denied, strip their links/derived data, and
  remove them from the config dictionary so re-ingest cannot revive them."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  pruned: list[str] = []
  missing: list[str] = []
  links = 0
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    for name in names:
      eid = resolve_entity_id(conn, name)
      if eid is None:
        missing.append(name)
        continue
      stats = prune_entity(conn, eid)
      links += stats["item_links"]
      pruned.append(name)
    # Drop pruned names from the config dictionary so seed_entities does not
    # flip them back to pending_review=0 on the next reseed.
    if pruned:
      entities_cfg = dict(cfg.get("entities") or {})
      dictionary = list(entities_cfg.get("dictionary") or [])
      pruned_lc = {n.casefold() for n in pruned}
      kept = [e for e in dictionary if str(e.get("canonical", "")).casefold() not in pruned_lc]
      if len(kept) != len(dictionary):
        entities_cfg["dictionary"] = kept
        _save_entities(config_path, entities_cfg)
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities prune", ok=True, stats={
      "pruned": len(pruned), "item_links": links, "missing": missing,
    }))
    return
  if pruned:
    click.echo(f"Pruned {len(pruned)} entit{'ies' if len(pruned) != 1 else 'y'} "
               f"({links} item links removed): {', '.join(pruned)}")
  if missing:
    click.echo(f"Not found: {', '.join(missing)}", err=True)


@entities_group.command("suggest-prune")
@config_option
@click.option("--max-items", default=None, type=int,
              help="Only flag entities with at most N item links (focus on low-traffic junk).")
@click.option("--limit", default=60, show_default=True, type=int, help="Max candidates to show.")
@click.option("--json", "as_json", is_flag=True, help="Raw candidates document on stdout.")
def entities_suggest_prune(config_path: str, max_items: int | None, limit: int, as_json: bool) -> None:
  """List NER entities that look like junk (common words, fragments, symbols)
  with the reasons they were flagged. Review, then remove with 'entities
  prune'. Nothing is changed."""
  import json as _json

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    candidates = _build_prune_candidates(conn, max_items=max_items)
  finally:
    conn.close()
  candidates = candidates[:limit]

  if as_json:
    click.echo(_json.dumps({"candidates": candidates}, ensure_ascii=False))
    return
  if not candidates:
    click.echo("No junk candidates found.")
    return
  click.echo(f"{len(candidates)} likely-junk entit{'ies' if len(candidates) != 1 else 'y'} "
             "(review, then: yaams entities prune ...):\n")
  for c in candidates:
    click.echo(f"  {c['name']!r}  ({c['items']} items)  [{', '.join(c['reasons'])}]")
  cmd = "yaams entities prune " + " ".join(
    _json.dumps(c["name"], ensure_ascii=False) for c in candidates
  )
  click.echo(f"\nPrune all shown:\n  {cmd}")


_ORG_SUFFIX = re.compile(
  r"\b(as|asa|ab|inc|llc|ltd|gmbh|group|gruppen|consulting|consult|holding|"
  r"norge|company|co|corp)\b",
  re.IGNORECASE,
)


def _norm_merge_key(name: str) -> str:
  """Normalize a name for grouping near-duplicates: lowercase, strip
  punctuation, drop common org suffixes, collapse whitespace."""
  s = name.lower()
  s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
  s = _ORG_SUFFIX.sub(" ", s)
  return re.sub(r"\s+", " ", s).strip()


@entities_group.command("suggest-merges")
@config_option
@click.option("--min-items", default=1, show_default=True, type=int,
              help="Ignore entities with fewer than N item links.")
@click.option("--json", "as_json", is_flag=True, help="Raw suggestions document on stdout.")
def entities_suggest_merges(config_path: str, min_items: int, as_json: bool) -> None:
  """Suggest entity-merge groups: entities whose names collapse to the same
  normalized key (e.g. Crayon / Crayon AS / Crayon Group)."""
  import json as _json

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    suggestions = _build_merge_suggestions(conn, min_items)
  finally:
    conn.close()

  for s in suggestions:
    s["command"] = "yaams entities merge " + " ".join(
      _json.dumps(x, ensure_ascii=False) for x in [s["survivor"], *s["victims"]]
    )

  if as_json:
    click.echo(_json.dumps({"suggestions": suggestions}, ensure_ascii=False))
    return
  if not suggestions:
    click.echo("No merge candidates found. For an interactive review run: "
               "yaams entities dedupe")
    return
  click.echo(f"{len(suggestions)} merge candidate group(s) "
             "(interactive: yaams entities dedupe):\n")
  for s in suggestions:
    members = ", ".join(f"{m['name']} ({m['items']})" for m in s["members"])
    click.echo(f"  • {members}")
    click.echo(f"    -> {s['command']}\n")


@entities_group.command("normalize")
@config_option
@click.option("--dry-run", is_flag=True, help="Show what would be merged without changing anything.")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def entities_normalize(config_path: str, dry_run: bool, as_json: bool) -> None:
  """Auto-merge entities that differ only by edge punctuation/whitespace
  (e.g. Hamas / Hamas', `Saksnavn / Saksnavn`). No review needed - these are
  unambiguously the same entity."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    result = normalize_entities(conn, dry_run=dry_run)
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities normalize", ok=True, stats={
      "merged": result["merged"], "renamed": result["renamed"],
      "groups": len(result["groups"]), "dry_run": dry_run,
    }))
    return
  if not result["groups"]:
    click.echo("Nothing to normalize — no punctuation-only variants found.")
    return
  verb = "Would merge" if dry_run else "Merged"
  for g in result["groups"]:
    if g["victims"]:
      click.echo(f"  {verb} {', '.join(g['victims'])} → '{g['survivor']}'")
    elif dry_run:
      click.echo(f"  Would clean '{g['survivor']}'")
  if dry_run:
    click.echo(f"\n{len(result['groups'])} group(s) would be normalized. "
               "Run without --dry-run to apply.")
  else:
    click.echo(f"\nNormalized {len(result['groups'])} group(s) "
               f"({result['merged']} merged, {result['renamed']} renamed). "
               "Run 'yaams assoc build' to refresh associations.")


@entities_group.command("vacuum")
@config_option
@click.option("--dry-run", is_flag=True, help="Count orphans without deleting.")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def entities_vacuum(config_path: str, dry_run: bool, as_json: bool) -> None:
  """Delete unreviewed NER entities that nothing references anymore (no item
  links, tags, meta, relations, associations, or promotion candidates).
  These pile up when a re-tag with a better model stops linking old junk.
  Curated and denied entities are never touched."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    result = vacuum_orphan_entities(conn, dry_run=dry_run)
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="entities vacuum", ok=True, stats={
      "orphans": result["orphans"], "deleted": result["deleted"],
      "dry_run": dry_run,
    }))
    return
  if not result["orphans"]:
    click.echo("No orphaned NER entities found.")
    return
  if dry_run:
    click.echo(f"{result['orphans']} orphaned NER entit(y/ies) would be deleted. "
               "Run without --dry-run to apply.")
  else:
    click.echo(f"Deleted {result['deleted']} orphaned NER entit(y/ies).")


@entities_group.command("dedupe")
@config_option
@click.option("--min-items", default=1, show_default=True, type=int,
              help="Ignore entities with fewer than N item links.")
@click.option("--no-normalize", is_flag=True,
              help="Skip the automatic punctuation-variant merge done first.")
@click.option("--json", "as_json", is_flag=True,
              help="(Rejected - dedupe is interactive; use 'entities suggest-merges --json'.)")
def entities_dedupe(config_path: str, min_items: int, no_normalize: bool, as_json: bool) -> None:
  """Interactively review entity-merge suggestions (curses TUI).

  Up/Down to move, [s] to change which entity survives, [m]/Enter to merge a
  group, [n] to skip (do-not-merge), [q] to quit. Merges are applied and made
  durable immediately."""
  if as_json:
    _reject_interactive_json(
      "entities dedupe",
      "Use `yaams entities suggest-merges --json` for the candidate list.",
    )
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  auto = {"merged": 0, "renamed": 0}
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    # Punctuation-only variants are unambiguous — merge them up front so the
    # interactive review only sees genuine judgment calls.
    if not no_normalize:
      auto = normalize_entities(conn)
    suggestions = _build_merge_suggestions(conn, min_items)
    if not suggestions:
      if auto["merged"] or auto["renamed"]:
        click.echo(f"Auto-normalized {auto['merged'] + auto['renamed']} "
                   "punctuation-only variant(s). No further candidates.")
      else:
        click.echo("No merge candidates found.")
      return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
      click.echo(
        f"{len(suggestions)} merge candidate(s). dedupe is interactive - run it "
        "in a terminal, or use `yaams entities suggest-merges` + "
        "`yaams entities merge`.",
        err=True,
      )
      sys.exit(EXIT_USER_ERROR)
    try:
      summary = _run_dedupe_tui(conn, config_path, suggestions)
    except RuntimeError as exc:
      click.echo(str(exc), err=True)
      sys.exit(EXIT_USER_ERROR)
  finally:
    conn.close()
  auto_n = auto["merged"] + auto["renamed"]
  prefix = f"Auto-normalized {auto_n} punctuation variant(s). " if auto_n else ""
  click.echo(f"{prefix}Done. Merged {summary['merged']}, skipped {summary['skipped']}.")
  if summary["merged"] or auto_n:
    click.echo("Next: 'yaams assoc build' to refresh associations.")


def _run_dedupe_tui(conn, config_path: str, suggestions: list[dict]) -> dict:
  """Drive merge suggestions interactively in curses. Applies each accepted
  merge immediately (config + DB). Returns {merged, skipped}."""
  try:
    import curses
  except ImportError as exc:  # pragma: no cover - platform dependent
    raise RuntimeError(
      "The dedupe TUI requires the stdlib 'curses' module, which is "
      "unavailable here. Use `yaams entities suggest-merges` + "
      "`yaams entities merge` instead."
    ) from exc

  state = {
    "groups": [
      {"members": list(s["members"]), "survivor_idx": 0} for s in suggestions
    ],
    "idx": 0,
    "merged": 0,
    "skipped": 0,
    "flash": "",
  }

  def _loop(stdscr):  # pragma: no cover - curses UI
    curses.curs_set(0)
    stdscr.keypad(True)
    try:
      curses.start_color()
      curses.use_default_colors()
      stdscr.bkgd(" ", curses.color_pair(0))
    except curses.error:
      pass

    while state["groups"]:
      state["idx"] = max(0, min(state["idx"], len(state["groups"]) - 1))
      _draw_dedupe(stdscr, state)
      ch = stdscr.getch()
      state["flash"] = ""

      if ch == curses.KEY_RESIZE:
        continue
      if ch in (ord("q"), 27):  # q / Esc
        break
      if ch in (curses.KEY_DOWN, ord("j"), ord("J")):
        state["idx"] = min(len(state["groups"]) - 1, state["idx"] + 1)
        continue
      if ch in (curses.KEY_UP, ord("k"), ord("K")):
        state["idx"] = max(0, state["idx"] - 1)
        continue

      g = state["groups"][state["idx"]]
      if ch in (ord("s"), ord("S"), curses.KEY_RIGHT, ord("\t")):
        g["survivor_idx"] = (g["survivor_idx"] + 1) % len(g["members"])
        continue
      if ch in (ord("n"), ord("N"), ord("x"), ord("X")):
        state["groups"].pop(state["idx"])
        state["skipped"] += 1
        state["flash"] = "skipped"
        continue
      if ch in (ord("m"), ord("M"), 10, 13):  # m / Enter
        members = g["members"]
        si = g["survivor_idx"]
        survivor = members[si]["name"]
        victims = [m["name"] for i, m in enumerate(members) if i != si]
        sid = resolve_entity_id(conn, survivor)
        vids = [resolve_entity_id(conn, v) for v in victims]
        vids = [v for v in vids if v is not None and v != sid]
        try:
          if sid is not None and vids:
            _n, stats, _a = _apply_merge(conn, config_path, sid, vids)
            state["merged"] += 1
            state["flash"] = (
              f"merged {stats['victims']} into '{survivor}' "
              f"({stats['item_links']} links)"
            )
          else:
            state["flash"] = "nothing to merge (entities no longer present)"
        except Exception as exc:  # keep the TUI alive on a failed merge
          state["flash"] = f"merge failed: {exc}"
        state["groups"].pop(state["idx"])
        continue

  try:
    curses.wrapper(_loop)
  except KeyboardInterrupt:  # pragma: no cover
    pass
  return {"merged": state["merged"], "skipped": state["skipped"]}


def _draw_dedupe(stdscr, state):  # pragma: no cover - curses UI
  import curses

  stdscr.erase()
  h, w = stdscr.getmaxyx()
  groups, idx = state["groups"], state["idx"]

  def line(y, text, attr=curses.A_NORMAL):
    if 0 <= y < h:
      try:
        stdscr.addstr(y, 0, text[: max(0, w - 1)], attr)
      except curses.error:
        pass

  line(0, f"yaams entities dedupe   {len(groups)} left · "
          f"{state['merged']} merged · {state['skipped']} skipped", curses.A_BOLD)
  line(1, "↑/↓ move · [s] change survivor · [m]/enter merge · [n] skip · [q] quit",
       curses.A_DIM)

  top = 3
  rows_per = 3
  per_page = max(1, (h - top - 1) // rows_per)
  page_start = (idx // per_page) * per_page
  y = top
  for gi in range(page_start, min(len(groups), page_start + per_page)):
    g = groups[gi]
    members, si = g["members"], g["survivor_idx"]
    survivor = members[si]
    victims = [m for i, m in enumerate(members) if i != si]
    selected = gi == idx
    marker = "➤ " if selected else "  "
    line(y, f"{marker}★ {survivor['name']} ({survivor['items']})",
         curses.A_REVERSE if selected else curses.A_BOLD)
    vtext = "      ← " + ", ".join(f"{v['name']} ({v['items']})" for v in victims)
    line(y + 1, vtext, curses.A_DIM)
    y += rows_per

  if state["flash"]:
    line(h - 1, state["flash"], curses.A_REVERSE)
  stdscr.refresh()
