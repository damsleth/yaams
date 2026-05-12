from __future__ import annotations

import click

from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.schema import init_schema
from yaams.signals import log_feedback, recent_queries

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option


@cli.command("feedback")
@click.argument("query_id")
@click.argument("kind", type=click.Choice(["hit", "miss", "correction", "note"]))
@click.option("--result", "result_id", default=None, help="Result id this feedback targets (omit for query-level)")
@click.option("--message", "-m", default=None, help="Free-text payload (e.g. \"expected X\" or correction details)")
@config_option
def feedback_cmd(
  query_id: str,
  kind: str,
  result_id: str | None,
  message: str | None,
  config_path: str,
) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    fid = log_feedback(
      conn,
      query_id=query_id,
      kind=kind,
      result_id=result_id,
      payload=message,
    )
  finally:
    conn.close()
  click.echo(f"Logged {kind} feedback for {query_id} (id={fid})")


@cli.command("signals")
@config_option
@click.option("--limit", default=20, show_default=True, type=int)
def signals_cmd(config_path: str, limit: int) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    rows = recent_queries(conn, limit=limit)
  finally:
    conn.close()
  if not rows:
    click.echo("No queries logged yet.")
    return
  click.echo(f"Last {len(rows)} queries:")
  for row in rows:
    backend = row.get("backend") or "-"
    latency = row.get("latency_ms")
    latency_s = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "-"
    click.echo(
      f"  {row['ts']}  {row['id']}  results={row['results_returned']:>2}  "
      f"latency={latency_s:>7}  backend={backend}  text={row['text'][:80]!r}"
    )
