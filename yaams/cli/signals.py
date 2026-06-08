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
from yaams.signals import flush_session, log_feedback, noise_cascade, recent_queries


@cli.command("feedback")
@click.argument("query_id")
@click.argument("kind", type=click.Choice(["hit", "miss", "correction", "relevant", "thin", "note", "noise"]))
@click.option("--result", "result_id", default=None, help="Result id this feedback targets (omit for query-level)")
@click.option("--message", "-m", default=None, help="Free-text payload (e.g. \"expected X\" or correction details)")
@click.option("--cascade/--no-cascade", default=True, show_default=True, help="For kind=noise, also mark every unjudged query with identical text.")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
@config_option
def feedback_cmd(
  query_id: str,
  kind: str,
  result_id: str | None,
  message: str | None,
  cascade: bool,
  as_json: bool,
  config_path: str,
) -> None:
  t0 = time.monotonic()
  cascaded_count = 0
  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    conn = open_db(db_path)
    try:
      init_schema(conn, embedding_dim=_embedding_dim(cfg))
      if kind == "noise" and cascade:
        row = conn.execute(
          "SELECT text FROM queries WHERE id = ?", (query_id,)
        ).fetchone()
        if row is None:
          raise click.ClickException(f"No query with id={query_id!r}")
        text = row["text"] if hasattr(row, "keys") else row[0]
        entries = noise_cascade(conn, query_id=query_id, text=text)
        cascaded_count = flush_session(conn, entries)
        # Synthesize an fid for the envelope by re-reading the latest row
        # for this query_id. log_feedback returns lastrowid per call; we
        # surface the cascade count instead.
        last = conn.execute(
          "SELECT id FROM query_feedback WHERE query_id = ? ORDER BY id DESC LIMIT 1",
          (query_id,),
        ).fetchone()
        fid = int(last[0]) if last else 0
      else:
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
    stats = {"feedback_id": fid, "query_id": query_id, "kind": kind, "result_id": result_id}
    if kind == "noise" and cascade:
      stats["cascaded"] = cascaded_count
    emit_action(action_envelope(
      command="feedback", ok=True,
      stats=stats,
      duration_ms=duration_ms,
    ))
    return
  if kind == "noise" and cascade:
    click.echo(f"Logged noise feedback for {query_id} (cascaded to {cascaded_count} row(s))")
  else:
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
