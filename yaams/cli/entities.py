from __future__ import annotations

import sys
import time

import click

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option
from yaams.config import get_db_path, load_config
from yaams.conventions import (
  EXIT_USER_ERROR,
  action_envelope,
  data_error,
  emit_action,
  emit_data_error,
)
from yaams.db import open_db
from yaams.schema import init_schema
from yaams.store import (
  add_entity_tags,
  backfill_entity_sources,
  get_entity_meta,
  get_entity_tags,
  remove_entity_meta,
  remove_entity_tags,
  resolve_entity_id,
  seed_entities,
  set_entity_meta,
)


def _reject_interactive_json(command: str, alt_hint: str) -> None:
  click.echo(
    f"{command} is an interactive command; --json is rejected. {alt_hint}",
    err=True,
  )
  sys.exit(EXIT_USER_ERROR)


def _save_entities(config_path: str | None, entities_cfg: dict) -> None:
  import re

  import yaml

  from yaams.config import resolve_config_path
  p = resolve_config_path(config_path)
  text = p.read_text(encoding="utf-8")
  block = yaml.dump({"entities": entities_cfg}, default_flow_style=False, allow_unicode=True, sort_keys=False)
  new_text = re.sub(r"^entities:.*?(?=^\S|\Z)", block, text, flags=re.MULTILINE | re.DOTALL)
  if new_text == text:
    new_text = text.rstrip() + "\n\n" + block
  p.write_text(new_text, encoding="utf-8")


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
      SELECT e.canonical_name, e.entity_type, count(*) AS cnt
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

    _NOISE = {
      # pronouns / function words (NO)
      "var", "hvordan", "ikke", "men", "inn", "deg", "meg", "jeg", "oss",
      "noe", "det", "den", "han", "hun", "her", "der", "fra", "til", "via",
      "ved", "som", "for", "alle", "noen", "hva", "når", "hvor", "også",
      # pronouns / function words (EN)
      "nice", "eta", "faks", "unett",
      # temporal terms (NO + EN) - not useful as entities
      "yesterday", "today", "tomorrow", "monday", "tuesday", "wednesday",
      "thursday", "friday", "saturday", "sunday",
      "januar", "februar", "mars", "april", "mai", "juni",
      "juli", "august", "september", "oktober", "november", "desember",
      "january", "february", "march", "june", "july", "october",
      "november", "december",
      "mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag",
      "igår", "idag", "imorgen", "uke", "måned", "år", "week", "month", "year",
      "morning", "evening", "night", "afternoon",
    }
    candidates = [
      r for r in rows
      if r["canonical_name"].lower() not in known
      and r["canonical_name"].lower() not in _NOISE
      and not r["canonical_name"].islower()
      and len(r["canonical_name"]) > 2
      and not r["canonical_name"].isdigit()
    ][:limit]

    if not candidates:
      click.echo(f"No NER candidates with {min_count}+ appearances not already in dictionary.")
      return

    click.echo(f"Found {len(candidates)} candidates.  [a]ccept  [e]dit  [d]eny  [q]uit\n")

    for i, row in enumerate(candidates, 1):
      canonical = row["canonical_name"]
      etype = row["entity_type"]
      cnt = row["cnt"]

      samples = conn.execute(
        """
        SELECT i.content, i.source, i.timestamp
        FROM item_entities ie
        JOIN entities e ON e.id = ie.entity_id
        JOIN items i ON i.id = ie.item_id
        WHERE e.canonical_name = ? AND ie.source = 'ner'
        ORDER BY i.timestamp DESC
        LIMIT 2
        """,
        (canonical,),
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
              WHERE lower(canonical_name) = lower(?)
              """,
              (canonical,),
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
          if any(e["canonical"].lower() == new_canonical.lower() for e in dictionary):
            click.echo(f"  '{new_canonical}' already in dictionary.")
            break
          entry: dict = {"canonical": new_canonical, "type": new_type}
          if aliases:
            entry["aliases"] = aliases
          dictionary.append(entry)
          entities_cfg["dictionary"] = dictionary
          _save_entities(config_path, entities_cfg)
          cfg = load_config(config_path)
          known.add(new_canonical.lower())
          d = cfg.get("entities", {}).get("dictionary", [])
          seed_entities(conn, d)
          backfill_entity_sources(conn, d)
          click.echo(f"  Added '{new_canonical}'.")
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
