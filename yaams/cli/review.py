"""``yaams review`` — scan-and-judge feedback over logged queries.

Defaults to a curses TUI when stdin is a TTY. ``--queue`` / ``--stats``
print non-interactive text views; ``--json`` emits a machine document.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import click

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option
from yaams.config import get_db_path, load_config
from yaams.conventions import (
  EXIT_USER_ERROR,
  data_error,
  emit_data_error,
)
from yaams.db import open_db
from yaams.schema import init_schema
from yaams.signals import (
  build_review_queue,
  dashboard_data,
  render_dashboard,
  run_review_tui,
)


@cli.command("review")
@config_option
@click.option("--since", default=None, help="Only include queries logged at/after this ISO timestamp.")
@click.option("--source", default=None, help="Restrict to queries with this source in source_filter.")
@click.option("--limit", default=50, show_default=True, type=int, help="Cap queue length.")
@click.option("--unjudged-only/--all", default=True, show_default=True, help="Skip queries that already have feedback.")
@click.option("--deferred", "deferred_only", is_flag=True, default=False, help="Surface only deferred queries (marked ? in the TUI).")
@click.option("--queue", "as_queue", is_flag=True, help="Print the prioritized queue as text and exit.")
@click.option("--stats", "as_stats", is_flag=True, help="Print the dashboard and exit.")
@click.option("--json", "as_json", is_flag=True, help="Emit the queue or dashboard as JSON on stdout.")
@click.option("--tui/--no-tui", default=None, help="Force TUI on/off. Default: TUI when stdin is a TTY.")
def review_cmd(
  config_path: str,
  since: str | None,
  source: str | None,
  limit: int,
  unjudged_only: bool,
  deferred_only: bool,
  as_queue: bool,
  as_stats: bool,
  as_json: bool,
  tui: bool | None,
) -> None:
  """Walk the unjudged-query queue and log hit/miss/correction verdicts.

  Default behavior: interactive curses TUI. Override with ``--queue`` to
  dump the queue as text, ``--stats`` for the dashboard, or ``--json``
  for a machine document.
  """
  # Decide mode up front. Explicit flags always win over the TTY check.
  noninteractive_explicit = as_queue or as_stats or as_json
  if tui is None:
    use_tui = sys.stdin.isatty() and sys.stdout.isatty() and not noninteractive_explicit
  else:
    use_tui = tui

  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    # Only the TUI writes (flush_session at the end of a judging run).
    # Queue/stats/JSON views open read-only so they work against a live DB
    # another process holds open — init_schema would need write access.
    conn = open_db(db_path, readonly=not use_tui)
    if use_tui:
      init_schema(conn, embedding_dim=_embedding_dim(cfg))
  except Exception as exc:
    if as_json:
      emit_data_error(data_error(
        command="review", code="db_open_failed", message=str(exc),
        hint="Run: yaams init-db",
      ))
      sys.exit(EXIT_USER_ERROR)
    raise

  try:
    if as_stats and not as_queue:
      data = dashboard_data(conn)
      if as_json:
        click.echo(json.dumps(data, ensure_ascii=False))
      else:
        click.echo(render_dashboard(data))
      return

    queue = build_review_queue(
      conn,
      since=since,
      source=source,
      limit=limit,
      unjudged_only=unjudged_only,
      deferred_only=deferred_only,
    )

    if use_tui:
      summary = run_review_tui(conn, queue)
      _print_session_summary(conn, summary)
      return

    if as_json:
      payload = _queue_payload(queue, limit=limit, unjudged_only=unjudged_only)
      click.echo(json.dumps(payload, ensure_ascii=False, default=str))
      return
  except sqlite3.OperationalError as exc:
    # Read-only mode can't create missing tables — surface "no such table"
    # (uninitialized DB) as a structured error instead of a traceback.
    if as_json:
      emit_data_error(data_error(
        command="review", code="db_query_failed", message=str(exc),
        hint="Run: yaams init-db",
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
  finally:
    conn.close()

  if not queue:
    click.echo("Review queue is empty.")
    return

  click.echo(f"Review queue ({len(queue)} {'query' if len(queue) == 1 else 'queries'}):")
  for i, item in enumerate(queue, 1):
    click.echo("")
    click.echo(
      f"{i:3d}. [{item.priority:.2f}] {item.query_id}  ({item.reason})"
    )
    click.echo(f"     {item.ts}  results={item.results_returned}  shape={item.shape or '-'}  conf={item.confidence or '-'}")
    text = item.text or ""
    if len(text) > 100:
      text = text[:99] + "…"
    click.echo(f"     Q: {text!r}")
    for r in item.results:
      cited = "★" if r.cited else " "
      snippet = r.snippet[:80]
      click.echo(f"       {cited} {r.rank}. [{r.source or '-'}] {snippet}")
  click.echo("")
  click.echo(
    "Log a verdict with: yaams feedback <query_id> <kind> [--result <id>]\n"
    "  answer-shaped (factual/first/last/event): hit | miss | correction\n"
    "  recall-shaped (synthesis/temporal_range): relevant | thin\n"
    "  any shape: noise"
  )


def _queue_payload(queue, *, limit: int, unjudged_only: bool) -> dict:
  return {
    "queries": [
      {
        "query_id": item.query_id,
        "text": item.text,
        "ts": item.ts,
        "results_returned": item.results_returned,
        "shape": item.shape,
        "confidence": item.confidence,
        "cited_count": item.cited_count,
        "priority": item.priority,
        "reasons": item.reasons,
        "results": [
          {
            "rank": r.rank,
            "result_id": r.result_id,
            "kind": r.kind,
            "source": r.source,
            "rrf_score": r.rrf_score,
            "snippet": r.snippet,
            "sender": r.sender,
            "timestamp": r.timestamp,
            "cited": r.cited,
          }
          for r in item.results
        ],
      }
      for item in queue
    ],
    "limit": limit,
    "unjudged_only": unjudged_only,
  }


def _print_session_summary(conn, summary: dict) -> None:
  judged = summary.get("judged", 0)
  if summary.get("aborted"):
    click.echo("Aborted — no verdicts logged.")
    return
  if not judged:
    click.echo("No verdicts logged.")
  else:
    click.echo(f"Logged {judged} verdict(s).")
  try:
    click.echo("")
    click.echo(render_dashboard(dashboard_data(conn)))
  except Exception:  # pragma: no cover - dashboard render is best-effort
    pass
