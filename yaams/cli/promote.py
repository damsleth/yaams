from __future__ import annotations

from pathlib import Path

import click

from yaams.config import expand_path, get_db_path, load_config
from yaams.db import open_db
from yaams.schema import init_schema

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option


@cli.group("promote")
def promote_group() -> None:
  """Generate and review promotion candidates for the Tier 2 ledger."""
  pass


@promote_group.command("generate")
@config_option
@click.option("--days", default=None, type=int, help="Override window_days from config")
@click.option("--min-cluster", default=None, type=int, help="Override min_cluster_items")
@click.option("--entity", default=None, help="Generate for a single entity name only")
def promote_generate(
  config_path: str,
  days: int | None,
  min_cluster: int | None,
  entity: str | None,
) -> None:
  from yaams.promote.candidates import PromoteConfig, generate_candidates, store_candidates
  from yaams.synthesize import llm_adapter_from_config

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  promote_cfg_raw = cfg.get("promote", {}) or {}
  raw_index_path = (
    cfg.get("ingest", {}).get("tier2_ledger", {}).get("index_path")
    or promote_cfg_raw.get("note_index_path")
  )
  pcfg = PromoteConfig(
    window_days=days or int(promote_cfg_raw.get("window_days", 90)),
    window_days_by_type=dict(promote_cfg_raw.get("window_days_by_type") or {"person": 365}),
    min_cluster_items=min_cluster or int(promote_cfg_raw.get("min_cluster_items", 3)),
    cluster_fetch_k=int(promote_cfg_raw.get("cluster_fetch_k", 10)),
    note_index_path=Path(raw_index_path) if raw_index_path else None,
  )
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    adapter = llm_adapter_from_config(cfg)
    click.echo(f"Generating candidates (window={pcfg.window_days}d, min_cluster={pcfg.min_cluster_items}) ...")
    candidates = generate_candidates(conn, adapter, pcfg, entity_filter=entity, on_progress=click.echo)
    stored = store_candidates(conn, candidates)
    click.echo(f"\nGenerated {len(candidates)} candidates, {stored} new stored.")
  finally:
    conn.close()


@promote_group.command("list")
@config_option
@click.option(
  "--status",
  default="pending",
  type=click.Choice(["pending", "accepted", "rejected", "all"]),
  show_default=True,
)
def promote_list(config_path: str, status: str) -> None:
  from yaams.promote.candidates import fetch_pending

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    rows = fetch_pending(conn, status)
  finally:
    conn.close()

  if not rows:
    click.echo(f"No candidates with status={status!r}.")
    return

  click.echo(f"{len(rows)} candidate(s) [{status}]:")
  for r in rows:
    click.echo(
      f"  {r['id'][:8]}  {r['status']:<10}  {r['draft_type']:<12}  "
      f"entity={r['entity']}  title={r['draft_title'][:50]}"
    )


@promote_group.command("review")
@config_option
@click.option("--all", "review_all", is_flag=True, help="Review all statuses, not just pending")
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="(Rejected - promote review is interactive; use 'promote list --json' for machine output.)",
)
def promote_review(config_path: str, review_all: bool, as_json: bool) -> None:
  if as_json:
    import sys
    click.echo(
      "promote review is an interactive command; --json is rejected. "
      "Use `yaams promote list --json` for machine-readable candidate data.",
      err=True,
    )
    sys.exit(1)

  from yaams.promote.candidates import (
    fetch_pending, mark_items_promoted, update_status,
  )
  from yaams.promote.review import format_note, render_candidate, write_to_inbox

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  promote_cfg_raw = cfg.get("promote", {}) or {}
  inbox_path = expand_path(
    promote_cfg_raw.get("inbox_path", "~/yaams/ledger-inbox")
  )

  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    status_filter = "all" if review_all else "pending"
    candidates = fetch_pending(conn, status_filter)
    if not candidates:
      click.echo("No candidates to review.")
      return

    total = len(candidates)
    for i, c in enumerate(candidates, 1):
      click.echo(render_candidate(c, i, total))
      while True:
        choice = click.prompt(
          "  [a]ccept  [e]dit  [r]eject  [s]kip  [q]uit",
          default="s",
          prompt_suffix=" > ",
        ).strip().lower()

        if choice == "q":
          click.echo("Review stopped.")
          return

        if choice == "s":
          break

        if choice == "r":
          update_status(conn, c["id"], "rejected")
          click.echo("  Rejected.")
          break

        if choice in ("a", "e"):
          note_content = format_note(c)
          if choice == "e":
            note_content = click.edit(note_content) or note_content
          dest = write_to_inbox(c, inbox_path, content=note_content)
          import json as _j
          try:
            item_ids = _j.loads(c.get("source_item_ids") or "[]")
          except Exception:
            item_ids = []
          mark_items_promoted(conn, item_ids, str(dest))
          update_status(conn, c["id"], "accepted", promoted_path=str(dest))
          click.echo(f"  Accepted -> {dest}")
          break

        click.echo("  Unknown choice. Use a/e/r/s/q.")

    click.echo("Review complete.")
  finally:
    conn.close()
