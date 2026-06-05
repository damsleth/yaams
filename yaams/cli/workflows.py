from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from typing import Any

import click

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, _entity_dictionary, config_option
from yaams.config import get_db_path, load_config
from yaams.conventions import (
  EXIT_USER_ERROR,
  action_envelope,
  emit_action,
)
from yaams.db import open_db
from yaams.retrieve.associate import build_cooccurrence
from yaams.schema import init_schema
from yaams.store import (
  backfill_entity_sources,
  normalize_entities,
  seed_entities,
  vacuum_orphan_entities,
)


def _reject_interactive_json(command: str, alt_hint: str) -> None:
  click.echo(
    f"{command} is an interactive command; --json is rejected. {alt_hint}",
    err=True,
  )
  sys.exit(EXIT_USER_ERROR)


def _safe_maintenance(
  config_path: str | None,
  *,
  dry_run: bool,
  require_vec: bool,
  build_assoc: bool,
  min_cooccur: int,
  min_score: float,
) -> dict[str, Any]:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, require_vec=require_vec)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    dictionary = _entity_dictionary(cfg)
    seed_entities(conn, dictionary)
    upgraded = backfill_entity_sources(conn, dictionary)
    dictionary_cleanup = None
    if not dry_run:
      from yaams.cli.ingest import _cleanup_entity_dictionary

      dictionary_cleanup = _cleanup_entity_dictionary(cfg)
    normalized = normalize_entities(conn, dry_run=dry_run)
    vacuumed = vacuum_orphan_entities(conn, dry_run=dry_run)
    assoc: dict[str, Any] = {"ran": False}
    if build_assoc:
      if dry_run:
        assoc = {"ran": False, "reason": "dry_run"}
      else:
        assoc = {
          "ran": True,
          "pairs": build_cooccurrence(
            conn,
            min_cooccur=min_cooccur,
            min_score=min_score,
          ),
          "min_cooccur": min_cooccur,
          "min_score": min_score,
        }
    return {
      "dictionary_links_upgraded": upgraded,
      "dictionary_cleanup": dictionary_cleanup,
      "normalize": {
        "merged": normalized["merged"],
        "renamed": normalized["renamed"],
        "groups": len(normalized["groups"]),
        "dry_run": dry_run,
      },
      "vacuum": {
        "orphans": vacuumed["orphans"],
        "deleted": vacuumed["deleted"],
        "dry_run": dry_run,
      },
      "assoc": assoc,
    }
  finally:
    conn.close()


def _print_maintenance(stats: dict[str, Any]) -> None:
  norm = stats["normalize"]
  vac = stats["vacuum"]
  click.echo("Safe maintenance complete.")
  click.echo(
    f"  Dictionary links upgraded: {stats['dictionary_links_upgraded']:,}"
  )
  cleanup = stats.get("dictionary_cleanup")
  if cleanup is not None:
    click.echo(
      f"  Entity dictionary: deduped {cleanup['dropped']} duplicate(s), "
      f"merged {cleanup['aliases_merged']} alias(es)"
    )
  click.echo(
    f"  Normalized: {norm['groups']} group(s), "
    f"{norm['merged']} merged, {norm['renamed']} renamed"
  )
  click.echo(
    f"  Vacuum: {vac['orphans']} orphan(s), {vac['deleted']} deleted"
  )
  assoc = stats.get("assoc") or {}
  if assoc.get("ran"):
    click.echo(f"  Associations: built {assoc['pairs']} pair(s)")
  elif assoc.get("reason") == "dry_run":
    click.echo("  Associations: skipped for dry run")


def _last_result_event(output: str) -> dict[str, Any] | None:
  for raw in reversed(output.splitlines()):
    try:
      payload = json.loads(raw)
    except json.JSONDecodeError:
      continue
    if payload.get("type") == "result":
      return payload
  return None


@cli.command("refresh")
@click.pass_context
@config_option
@click.option("--source", default="all", show_default=True, help="Source selector passed through to ingest.")
@click.option("--dry-run", is_flag=True, help="Fetch and plan without writing ingest or maintenance changes.")
@click.option("--batch-size", default=64, show_default=True)
@click.option("--require-vec", is_flag=True)
@click.option("-v", "--verbose", is_flag=True, help="Stream DEBUG logs during ingest.")
@click.option("--strict", is_flag=True, help="Pass through to ingest.")
@click.option("--skip-ingest", is_flag=True, help="Only run safe maintenance.")
@click.option("--skip-assoc", is_flag=True, help="Skip learned association rebuild.")
@click.option("--assoc-min-cooccur", default=3, show_default=True, type=int)
@click.option("--assoc-min-score", default=0.15, show_default=True, type=float)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def refresh(
  ctx: click.Context,
  config_path: str,
  source: str,
  dry_run: bool,
  batch_size: int,
  require_vec: bool,
  verbose: bool,
  strict: bool,
  skip_ingest: bool,
  skip_assoc: bool,
  assoc_min_cooccur: int,
  assoc_min_score: float,
  as_json: bool,
) -> None:
  """Ingest enabled sources, then run unattended safe maintenance."""
  start = time.perf_counter()
  try:
    load_config(config_path)
  except Exception as exc:
    if as_json:
      emit_action(action_envelope(
        command="refresh",
        ok=False,
        error={"code": "config_unreadable", "message": str(exc)},
        duration_ms=(time.perf_counter() - start) * 1000,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise

  ingest_exit_code = 0
  ingest_result = None
  if not skip_ingest:
    from yaams.cli.ingest import ingest as ingest_cmd

    if as_json:
      out = io.StringIO()
      err = io.StringIO()
      ingest_exit_code = 0
      with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
          ctx.invoke(
            ingest_cmd,
            config_path=config_path,
            source=source,
            dry_run=dry_run,
            batch_size=batch_size,
            require_vec=require_vec,
            verbose=verbose,
            as_json=True,
            strict=strict,
          )
        except SystemExit as exc:
          ingest_exit_code = int(exc.code or 0)
      ingest_result = _last_result_event(out.getvalue())
    else:
      click.echo("== Ingest ==")
      ctx.invoke(
        ingest_cmd,
        config_path=config_path,
        source=source,
        dry_run=dry_run,
        batch_size=batch_size,
        require_vec=require_vec,
        verbose=verbose,
        as_json=False,
        strict=strict,
      )

  if not as_json:
    click.echo("\n== Maintenance ==")
  maintenance = _safe_maintenance(
    config_path,
    dry_run=dry_run,
    require_vec=require_vec,
    build_assoc=not skip_assoc,
    min_cooccur=assoc_min_cooccur,
    min_score=assoc_min_score,
  )
  duration_ms = (time.perf_counter() - start) * 1000
  if as_json:
    ok = ingest_exit_code == 0 if not skip_ingest else True
    emit_action(action_envelope(
      command="refresh",
      ok=ok,
      stats={
        "ingest_ran": not skip_ingest,
        "ingest": ingest_result,
        "source": source,
        "dry_run": dry_run,
        "maintenance": maintenance,
      },
      error=None if ok else {
        "code": "ingest_failed",
        "message": "Underlying ingest step failed",
        "exit_code": ingest_exit_code,
      },
      duration_ms=duration_ms,
    ))
    if not ok:
      sys.exit(ingest_exit_code or EXIT_USER_ERROR)
    return
  _print_maintenance(maintenance)


@cli.command("curate")
@click.pass_context
@config_option
@click.option("--min-items", default=1, show_default=True, type=int,
              help="Minimum item links for merge suggestions.")
@click.option("--max-prune-items", default=None, type=int,
              help="Only flag likely junk with at most N item links.")
@click.option("--prune-limit", default=50, show_default=True, type=int)
@click.option("--discover-min-count", default=5, show_default=True, type=int)
@click.option("--discover-limit", default=50, show_default=True, type=int)
@click.option("--skip-dedupe", is_flag=True, help="Skip the interactive merge TUI.")
@click.option("--skip-discover", is_flag=True, help="Skip interactive candidate discovery.")
@click.option("--skip-assoc", is_flag=True, help="Skip learned association rebuild at the end.")
@click.option("--json", "as_json", is_flag=True,
              help="(Rejected - curate is interactive; use primitive commands with --json.)")
def curate(
  ctx: click.Context,
  config_path: str,
  min_items: int,
  max_prune_items: int | None,
  prune_limit: int,
  discover_min_count: int,
  discover_limit: int,
  skip_dedupe: bool,
  skip_discover: bool,
  skip_assoc: bool,
  as_json: bool,
) -> None:
  """Run the human entity curation workflow."""
  if as_json:
    _reject_interactive_json(
      "curate",
      "Use `entities suggest-merges --json`, `entities suggest-prune --json`, "
      "and `entities discover` separately.",
    )

  click.echo("== Safe maintenance ==")
  maintenance = _safe_maintenance(
    config_path,
    dry_run=False,
    require_vec=False,
    build_assoc=False,
    min_cooccur=3,
    min_score=0.15,
  )
  _print_maintenance(maintenance)

  from yaams.cli.entities import (
    entities_dedupe,
    entities_discover,
    entities_suggest_merges,
    entities_suggest_prune,
  )

  click.echo("\n== Merge suggestions ==")
  ctx.invoke(entities_suggest_merges, config_path=config_path, min_items=min_items, as_json=False)

  click.echo("\n== Prune suggestions ==")
  ctx.invoke(
    entities_suggest_prune,
    config_path=config_path,
    max_items=max_prune_items,
    limit=prune_limit,
    as_json=False,
  )

  interactive = sys.stdin.isatty() and sys.stdout.isatty()
  if not skip_dedupe:
    if interactive:
      click.echo("\n== Interactive dedupe ==")
      ctx.invoke(
        entities_dedupe,
        config_path=config_path,
        min_items=min_items,
        no_normalize=True,
        as_json=False,
      )
    else:
      click.echo("\nInteractive dedupe skipped because stdin/stdout is not a TTY.")

  if not skip_discover:
    if interactive:
      click.echo("\n== Entity discovery ==")
      ctx.invoke(
        entities_discover,
        config_path=config_path,
        min_count=discover_min_count,
        limit=discover_limit,
        as_json=False,
      )
    else:
      click.echo("Interactive discover skipped because stdin/stdout is not a TTY.")

  if not skip_assoc:
    from yaams.retrieve.associate import build_cooccurrence

    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    conn = open_db(db_path)
    try:
      init_schema(conn, embedding_dim=_embedding_dim(cfg))
      pairs = build_cooccurrence(conn)
    finally:
      conn.close()
    click.echo(f"\nAssociations rebuilt: {pairs} pair(s).")
