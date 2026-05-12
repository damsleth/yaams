from __future__ import annotations

import click

from yaams.config import get_db_path, load_config
from yaams.consolidate import Consolidation, SessionConfig, build_consolidations
from yaams.db import open_db
from yaams.schema import init_schema
from yaams.store import (
  clear_consolidations,
  consolidation_stats,
  fetch_items_for_consolidation,
  store_consolidations,
)

from yaams.cli._root import cli
from yaams.cli._shared import ProcessingContext, _embedding_dim, config_option


@cli.command("consolidate")
@config_option
@click.option(
  "--source",
  default="all",
  show_default=True,
  help="all, imessage, teams, or a specific teams_<profile>",
)
@click.option("--dry-run", is_flag=True)
@click.option("--rebuild", is_flag=True, help="Clear existing consolidations first")
@click.option("--require-vec", is_flag=True)
def consolidate(
  config_path: str,
  source: str,
  dry_run: bool,
  rebuild: bool,
  require_vec: bool,
) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, require_vec=require_vec)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    sources = _consolidation_sources(source, conn)
    if not sources:
      click.echo("No conversational sources to consolidate.")
      return

    cons_cfg = SessionConfig(**_consolidation_config(cfg))

    if rebuild and not dry_run:
      cleared = clear_consolidations(conn, sources)
      click.echo(f"Cleared {cleared:,} existing consolidations.")

    processors = None if dry_run else ProcessingContext(cfg)
    totals = {"sessions": 0, "raw_items": 0}
    click.echo("Consolidating sources:")
    for src in sources:
      items = fetch_items_for_consolidation(
        conn,
        src,
        only_unconsolidated=not rebuild,
      )
      consolidations = build_consolidations(items, cons_cfg)
      summary_items = sum(c.item_count for c in consolidations)
      click.echo(
        f"  {src}: {len(items):,} candidate items -> "
        f"{len(consolidations):,} consolidations ({summary_items:,} items folded)"
      )
      totals["sessions"] += len(consolidations)
      totals["raw_items"] += summary_items
      if dry_run or not consolidations:
        continue
      _persist_consolidations(conn, consolidations, processors)

    if not dry_run:
      conn.commit()
    click.echo(
      f"Total: {totals['sessions']:,} consolidations across "
      f"{totals['raw_items']:,} folded items"
    )
    db_stats = consolidation_stats(conn)
    click.echo(
      f"  DB now: {db_stats['total_consolidations']:,} consolidations covering "
      f"{db_stats['total_items_consolidated']:,} items"
    )
  finally:
    conn.close()


def _consolidation_sources(requested: str, conn) -> list[str]:
  rows = conn.execute(
    """
    SELECT DISTINCT source FROM items
    WHERE source = 'imessage' OR source LIKE 'teams_%'
    ORDER BY source
    """
  ).fetchall()
  available = [row["source"] for row in rows]
  if requested == "all":
    return available
  if requested == "teams":
    return [s for s in available if s.startswith("teams_")]
  if requested == "imessage":
    return [s for s in available if s == "imessage"]
  if requested in available:
    return [requested]
  return []


def _consolidation_config(cfg: dict) -> dict:
  raw = dict(cfg.get("consolidate", {}) or {})
  allowed = {"gap_minutes", "max_session_items", "min_session_items", "summary_max_chars"}
  return {k: int(v) for k, v in raw.items() if k in allowed}


def _persist_consolidations(
  conn,
  consolidations: list[Consolidation],
  processors,
) -> None:
  if processors is None:
    raise RuntimeError("processors required when persisting consolidations")
  texts = [c.summary for c in consolidations]
  embeddings = processors.embedder.embed_batch(texts)
  store_consolidations(conn, consolidations, embeddings=embeddings)
