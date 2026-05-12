from __future__ import annotations

import sys
import time

import click

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
from yaams.signals import log_feedback, recent_queries

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option


@cli.command("feedback")
@click.argument("query_id")
@click.argument("kind", type=click.Choice(["hit", "miss", "correction", "note"]))
@click.option("--result", "result_id", default=None, help="Result id this feedback targets (omit for query-level)")
@click.option("--message", "-m", default=None, help="Free-text payload (e.g. \"expected X\" or correction details)")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def feedback_cmd(
  query_id: str,
  kind: str,
  result_id: str | None,
  message: str | None,
  as_json: bool,
  config_path: str,
) -> None:
  t0 = time.monotonic()
  try:
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
  except Exception as exc:
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="feedback", ok=False,
        error={"code": "feedback_failed", "message": str(exc)},
        duration_ms=duration_ms,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="feedback", ok=True,
      stats={"feedback_id": fid, "query_id": query_id, "kind": kind, "result_id": result_id},
      duration_ms=duration_ms,
    ))
    return
  click.echo(f"Logged {kind} feedback for {query_id} (id={fid})")


@cli.command("signals")
@config_option
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True, help="Raw signals document on stdout.")
def signals_cmd(config_path: str, limit: int, as_json: bool) -> None:
  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    conn = open_db(db_path, readonly=True)
  except Exception as exc:
    if as_json:
      emit_data_error(data_error(
        command="signals", code="db_open_failed", message=str(exc),
        hint="Run: yaams init-db",
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
  try:
    rows = recent_queries(conn, limit=limit)
  finally:
    conn.close()
  if as_json:
    # Data-class success: raw document on stdout, NO top-level `ok`
    # (reserved-key contract). Wrap in `{"queries": [...]}` so the
    # consumer sees a stable shape regardless of whether the result
    # rows happen to contain an `ok` field.
    import json as _json
    click.echo(_json.dumps({"queries": list(rows), "limit": limit}, ensure_ascii=False, default=str))
    return
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
