from __future__ import annotations

import click

from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.schema import init_schema
from yaams.store import backfill_entity_sources, seed_entities

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option


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
def entities_list(config_path: str) -> None:
  """Show all dictionary entities with item hit counts."""
  cfg = load_config(config_path)
  dictionary = (cfg.get("entities") or {}).get("dictionary") or []
  if not dictionary:
    click.echo("No entities in dictionary. Add some with: entities add <name>")
    return
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
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
      alias_str = f"  aliases: {', '.join(aliases)}" if aliases else ""
      click.echo(f"  {canonical:<22} {etype:<12} {count:>5} items{alias_str}")
  finally:
    conn.close()


@entities_group.command("add")
@click.argument("canonical")
@click.option("--type", "etype", default="person", show_default=True)
@click.option("--alias", "aliases", multiple=True, help="Repeatable: --alias EP --alias Ex.Person")
@config_option
def entities_add(canonical: str, etype: str, aliases: tuple[str, ...], config_path: str) -> None:
  """Add an entity to the dictionary and seed the DB immediately."""
  cfg = load_config(config_path)
  entities_cfg = dict(cfg.get("entities") or {})
  dictionary = list(entities_cfg.get("dictionary") or [])
  if any(e["canonical"].lower() == canonical.lower() for e in dictionary):
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
  click.echo(f"Added '{canonical}' ({etype}).")


@entities_group.command("remove")
@click.argument("canonical")
@config_option
def entities_remove(canonical: str, config_path: str) -> None:
  """Remove an entity from the dictionary (existing DB links are kept)."""
  cfg = load_config(config_path)
  entities_cfg = dict(cfg.get("entities") or {})
  dictionary = list(entities_cfg.get("dictionary") or [])
  before = len(dictionary)
  dictionary = [e for e in dictionary if e["canonical"].lower() != canonical.lower()]
  if len(dictionary) == before:
    click.echo(f"'{canonical}' not found in dictionary.")
    return
  entities_cfg["dictionary"] = dictionary
  _save_entities(config_path, entities_cfg)
  click.echo(f"Removed '{canonical}'. Existing item links in the DB are untouched.")


@entities_group.command("discover")
@config_option
@click.option("--min-count", default=5, show_default=True, help="Minimum appearances to surface a candidate")
@click.option("--limit", default=50, show_default=True, help="Max candidates to review")
def entities_discover(config_path: str, min_count: int, limit: int) -> None:
  """Scan NER-tagged items and suggest new dictionary entries interactively."""
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
def entities_denied(config_path: str) -> None:
  """List previously denied NER candidates and optionally restore them."""
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
