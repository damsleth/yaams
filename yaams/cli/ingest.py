from __future__ import annotations

import sys
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import click

from yaams.cli import sources as sources_mod
from yaams.cli._root import cli
from yaams.cli._shared import (
  ProcessingContext,
  _date,
  _embedding_dim,
  _entity_dictionary,
  _format_duration,
  _format_throughput,
  _size_mb,
  config_option,
)
from yaams.config import get_db_path, load_config
from yaams.conventions import (
  EXIT_OK,
  EXIT_PARTIAL,
  EXIT_USER_ERROR,
  action_envelope,
  emit_action,
  stream_progress,
  stream_result,
)
from yaams.db import open_db
from yaams.ingest import Adapter, Item
from yaams.ingest.calendar import CalendarAdapter
from yaams.ingest.email_mbox import EmailAdapter
from yaams.ingest.folder import FolderAdapter
from yaams.ingest.github import GitHubAdapter
from yaams.ingest.imessage import IMessageAdapter
from yaams.ingest.ledger_notes import LedgerNotesAdapter
from yaams.ingest.m365_mail import M365MailAdapter
from yaams.ingest.obsidian import ObsidianAdapter
from yaams.ingest.signal import SignalAdapter
from yaams.ingest.teams import GraphClient, OwaPiggyTokenSource, TeamsAdapter
from yaams.ingest.teams_chatsvc import ChatsvcAdapter, ChatsvcClient
from yaams.logsetup import setup_logging
from yaams.schema import init_schema
from yaams.store import (
  backfill_entity_sources,
  database_stats,
  seed_entities,
  store_items,
)
from yaams.time import parse_iso_datetime
from yaams.watermark import get_watermark, update_watermark


@cli.command("ingest")
@config_option
@click.option(
  "--source",
  default="all",
  show_default=True,
  help=(
    "all, imessage, signal, email, notes, folders, tier2_ledger, github, "
    "teams or teams_<profile>, calendar or calendar_<profile>, "
    "mail or mail_<profile>"
  ),
)
@click.option("--dry-run", is_flag=True)
@click.option("--batch-size", default=64, show_default=True)
@click.option("--require-vec", is_flag=True)
@click.option("-v", "--verbose", is_flag=True, help="Stream DEBUG logs to stderr in addition to the log file.")
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="NDJSON progress on stdout + final action envelope on stdout.",
)
@click.option(
  "--strict",
  is_flag=True,
  help="Treat any source failure as fatal (exit 1 instead of partial-success exit 5).",
)
def ingest(
  config_path: str,
  source: str,
  dry_run: bool,
  batch_size: int,
  require_vec: bool,
  verbose: bool,
  as_json: bool,
  strict: bool,
) -> None:
  total_start = time.perf_counter()
  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
  except Exception as exc:
    if as_json:
      emit_action(action_envelope(
        command="ingest", ok=False,
        error={"code": "config_unreadable", "message": str(exc)},
        duration_ms=(time.perf_counter() - total_start) * 1000.0,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
  log_file = setup_logging(db_path, verbose=verbose)
  if log_file and not as_json:
    click.echo(f"Logging to {log_file}", err=True)
  conn = open_db(db_path, require_vec=require_vec)
  run_stats: dict[str, dict[str, object]] = defaultdict(
    lambda: {"seen": 0, "new": 0, "skipped": 0}
  )
  succeeded: list[str] = []
  failed_sources: list[str] = []
  run_id = uuid.uuid4().hex
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    seed_entities(conn, _entity_dictionary(cfg))
    backfill_entity_sources(conn, _entity_dictionary(cfg))
    processors = None if dry_run else ProcessingContext(cfg)
    sources_planned = [s for s in _sources_to_run(source, cfg) if _source_enabled(cfg, s)]
    if as_json:
      stream_progress(stage="plan", total=len(sources_planned))

    # `since` is read from the DB; resolve it on the main thread before
    # fanning out, since a sqlite connection can't be shared across threads.
    since_by_source = {src: _effective_since(conn, src, cfg) for src in sources_planned}

    # Phase 1 — fetch every source concurrently. Sources are network/IO-bound
    # (owa-* subprocesses, Graph round-trips) and independent, so wall-clock
    # collapses from the sum of per-source latency toward the slowest source.
    # No conn access here: only get_adapter() + adapter.extract().
    def _fetch_source(src: str) -> tuple:
      started_at = datetime.now(UTC)
      perf_start = time.perf_counter()
      try:
        adapter = get_adapter(src, cfg["ingest"][_config_section(src)])
        items = list(adapter.extract(since_by_source[src]))
        fetch_ms = (time.perf_counter() - perf_start) * 1000
        return src, adapter, items, started_at, fetch_ms, None
      except Exception as exc:  # reported per source in the serial phase
        fetch_ms = (time.perf_counter() - perf_start) * 1000
        return src, None, [], started_at, fetch_ms, exc

    fetched: dict[str, tuple] = {}
    if sources_planned:
      max_workers = min(8, len(sources_planned))
      if not as_json:
        click.echo(
          f"Fetching {len(sources_planned)} source(s), "
          f"up to {max_workers} in parallel...",
          err=True,
        )
      with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_source, src) for src in sources_planned]
        for fut in as_completed(futures):
          res = fut.result()
          fetched[res[0]] = res
          if not as_json:
            fsrc, _adapter, fitems, _started, _ms, ferr = res
            if ferr is None:
              click.echo(f"  fetched {fsrc}: {len(fitems)} item(s)", err=True)
            else:
              click.echo(
                f"  {fsrc}: fetch failed - {type(ferr).__name__}: {ferr}",
                err=True,
              )

    # Phase 2 — process serially in plan order. Embedding + SQLite writes
    # share one connection and the embedder batches best single-threaded.
    for src in sources_planned:
      _src, adapter, items, src_started_at, fetch_ms, src_error = fetched[src]
      if as_json:
        stream_progress(source=src, stage="start")
      source_stats = None
      if src_error is None:
        try:
          source_stats = ingest_source(
            conn,
            src,
            adapter,
            items,
            since_by_source[src],
            batch_size=batch_size,
            dry_run=dry_run,
            processors=processors,
            started_at=src_started_at,
            fetch_ms=fetch_ms,
          )
        except Exception as exc:
          src_error = exc
      if src_error is not None:
        duration_ms = fetch_ms
        error_text = f"{type(src_error).__name__}: {src_error}"
        if not as_json:
          click.echo(f"  {src}: failed - {error_text}", err=True)
        else:
          stream_progress(source=src, stage="failed", done=0)
        run_stats[src]["failed"] = error_text
        run_stats[src]["paths"] = _source_paths(src, cfg)
        run_stats[src]["duration_ms"] = duration_ms
        failed_sources.append(src)
        if not dry_run:
          _record_ingest_run(
            conn,
            run_id=run_id,
            source=src,
            started_at=src_started_at,
            ended_at=datetime.now(UTC),
            duration_ms=duration_ms,
            seen=0,
            new=0,
            skipped=0,
            status="failed",
            error=error_text,
          )
          conn.commit()
        continue
      run_stats[src]["seen"] += source_stats["seen"]
      run_stats[src]["new"] += source_stats["new"]
      run_stats[src]["skipped"] += source_stats["skipped"]
      run_stats[src]["files_walked"] = (
        int(run_stats[src].get("files_walked", 0) or 0)
        + int(source_stats.get("files_walked", 0) or 0)
      )
      run_stats[src]["skipped_before_cutoff"] = int(
        source_stats.get("skipped_before_cutoff", 0) or 0
      )
      run_stats[src]["skipped_emlx"] = source_stats["skipped_emlx"]
      run_stats[src]["skipped_email_dates"] = source_stats["skipped_email_dates"]
      run_stats[src]["skipped_newsletters"] = source_stats.get("skipped_newsletters", 0)
      run_stats[src]["skipped_bots"] = source_stats.get("skipped_bots", 0)
      run_stats[src]["skipped_system"] = source_stats.get("skipped_system", 0)
      run_stats[src]["skipped_empty"] = source_stats.get("skipped_empty", 0)
      run_stats[src]["skipped_no_timestamp"] = source_stats.get("skipped_no_timestamp", 0)
      run_stats[src]["decoded_attributed_body"] = source_stats[
        "decoded_attributed_body"
      ]
      run_stats[src]["skipped_attributed_body"] = source_stats[
        "skipped_attributed_body"
      ]
      run_stats[src]["since"] = source_stats["since"]
      run_stats[src]["paths"] = _source_paths(src, cfg)
      run_stats[src]["duration_ms"] = source_stats["duration_ms"]
      succeeded.append(src)
      if as_json:
        stream_progress(
          source=src,
          stage="done",
          done=int(source_stats["seen"]),
        )
      if not dry_run:
        _record_ingest_run(
          conn,
          run_id=run_id,
          source=src,
          started_at=parse_iso_datetime(str(source_stats["started_at"])),
          ended_at=parse_iso_datetime(str(source_stats["ended_at"])),
          duration_ms=float(source_stats["duration_ms"]),  # type: ignore[arg-type]
          seen=int(source_stats["seen"]),  # type: ignore[arg-type]
          new=int(source_stats["new"]),  # type: ignore[arg-type]
          skipped=int(source_stats["skipped"]),  # type: ignore[arg-type]
          status="success",
          error=None,
        )
        conn.commit()
    total_duration_ms = (time.perf_counter() - total_start) * 1000
    if as_json:
      envelope, exit_code = _build_ingest_envelope(
        run_stats=run_stats,
        succeeded=succeeded,
        failed_sources=failed_sources,
        sources_planned=sources_planned,
        dry_run=dry_run,
        total_duration_ms=total_duration_ms,
        strict=strict,
      )
      stream_result(envelope)
      conn.close()
      sys.exit(exit_code)
    print_stats(
      conn,
      db_path,
      run_stats,
      dry_run=dry_run,
      total_duration_ms=total_duration_ms,
    )
  finally:
    if conn is not None:
      try:
        conn.close()
      except Exception:
        pass


def _build_ingest_envelope(
  *,
  run_stats: dict[str, dict[str, object]],
  succeeded: list[str],
  failed_sources: list[str],
  sources_planned: list[str],
  dry_run: bool,
  total_duration_ms: float,
  strict: bool,
) -> tuple[dict, int]:
  """Return (envelope, exit_code) following the partial-success rules.

  - All sources succeeded -> exit 0, ok=true.
  - All sources failed -> exit 1, ok=false.
  - Mixed -> exit 5 (partial), collapsed to exit 1 under --strict.
  - No sources planned (nothing enabled) -> exit 0, ok=true with a warning.
  """
  per_source = {src: dict(stats) for src, stats in run_stats.items()}
  totals = {
    "seen": sum(int(s.get("seen", 0) or 0) for s in run_stats.values()),
    "new": sum(int(s.get("new", 0) or 0) for s in run_stats.values()),
    "skipped": sum(int(s.get("skipped", 0) or 0) for s in run_stats.values()),
  }
  stats = {
    "dry_run": dry_run,
    "sources_planned": sources_planned,
    "sources": per_source,
    "totals": totals,
  }
  warnings: list[str] = []
  if not sources_planned:
    warnings.append("No sources enabled in config.yaml")
    envelope = action_envelope(
      command="ingest", ok=True, stats=stats, warnings=warnings,
      duration_ms=total_duration_ms,
    )
    return envelope, EXIT_OK

  if not failed_sources:
    envelope = action_envelope(
      command="ingest", ok=True, stats=stats, warnings=warnings,
      duration_ms=total_duration_ms,
    )
    return envelope, EXIT_OK

  if not succeeded:
    # All failed.
    error = {
      "code": "all_sources_failed",
      "message": f"All {len(failed_sources)} planned source(s) failed",
      "failed_sources": failed_sources,
    }
    envelope = action_envelope(
      command="ingest", ok=False, stats=stats, warnings=warnings,
      error=error, duration_ms=total_duration_ms,
    )
    return envelope, EXIT_USER_ERROR

  # Partial success.
  if strict:
    error = {
      "code": "partial_failure_strict",
      "message": f"{len(failed_sources)} source(s) failed under --strict",
      "failed_sources": failed_sources,
    }
    envelope = action_envelope(
      command="ingest", ok=False, stats=stats, warnings=warnings,
      error=error, duration_ms=total_duration_ms,
    )
    return envelope, EXIT_USER_ERROR

  error = {
    "code": "partial_failure",
    "message": f"{len(failed_sources)} source(s) failed; {len(succeeded)} succeeded",
    "failed_sources": failed_sources,
  }
  envelope = action_envelope(
    command="ingest", ok=False, stats=stats, warnings=warnings,
    error=error, duration_ms=total_duration_ms,
  )
  return envelope, EXIT_PARTIAL


def ingest_source(
  conn,
  source: str,
  adapter: Adapter,
  items: list[Item],
  since: datetime,
  *,
  batch_size: int,
  dry_run: bool,
  processors,
  started_at: datetime,
  fetch_ms: float,
) -> dict[str, object]:
  """Embed + store an already-fetched item list and advance the watermark.

  Fetching (``adapter.extract``) happens upstream so sources can run
  concurrently; this half is serial because it shares one sqlite connection.
  ``fetch_ms`` is folded into the reported duration so per-source timings
  still reflect total work.
  """
  store_start = time.perf_counter()
  batch: list[Item] = []
  latest_ts = since
  seen = 0
  inserted = 0
  for item in items:
    seen += 1
    batch.append(item)
    if item.timestamp > latest_ts:
      latest_ts = item.timestamp
    if len(batch) >= batch_size:
      inserted += process_batch(conn, batch, processors, dry_run=dry_run)
      batch = []
  if batch:
    inserted += process_batch(conn, batch, processors, dry_run=dry_run)
  # Advance the watermark past messages that were scanned but deliberately
  # skipped (e.g. newsletters) so wide date windows aren't re-walked every
  # run. Adapters that can't bound their scan leave scanned_through unset.
  scanned_through = getattr(adapter, "scanned_through", None)
  if scanned_through is not None and scanned_through > latest_ts:
    latest_ts = scanned_through
  if not dry_run:
    update_watermark(conn, source, latest_ts)
    conn.commit()
  duration_ms = fetch_ms + (time.perf_counter() - store_start) * 1000
  files_walked = int(getattr(adapter, "files_walked", 0))
  skipped_before_cutoff = int(getattr(adapter, "skipped_before_cutoff", 0))
  return {
    "seen": seen,
    "new": inserted,
    "files_walked": files_walked,
    "skipped_before_cutoff": skipped_before_cutoff,
    "skipped": int(getattr(adapter, "skipped_emlx", 0))
    + int(getattr(adapter, "skipped_email_dates", 0))
    + int(getattr(adapter, "skipped_newsletters", 0))
    + int(getattr(adapter, "skipped_bots", 0))
    + int(getattr(adapter, "skipped_system", 0))
    + int(getattr(adapter, "skipped_empty", 0))
    + int(getattr(adapter, "skipped_no_timestamp", 0))
    + skipped_before_cutoff,
    "skipped_emlx": int(getattr(adapter, "skipped_emlx", 0)),
    "skipped_email_dates": int(getattr(adapter, "skipped_email_dates", 0)),
    "skipped_newsletters": int(getattr(adapter, "skipped_newsletters", 0)),
    "skipped_bots": int(getattr(adapter, "skipped_bots", 0)),
    "skipped_system": int(getattr(adapter, "skipped_system", 0)),
    "skipped_empty": int(getattr(adapter, "skipped_empty", 0)),
    "skipped_no_timestamp": int(getattr(adapter, "skipped_no_timestamp", 0)),
    "decoded_attributed_body": int(getattr(adapter, "decoded_attributed_body", 0)),
    "skipped_attributed_body": int(getattr(adapter, "skipped_attributed_body", 0)),
    "since": since.isoformat(),
    "started_at": started_at.isoformat(),
    "ended_at": datetime.now(UTC).isoformat(),
    "duration_ms": duration_ms,
  }


def process_batch(
  conn,
  items: list[Item],
  processors,
  *,
  dry_run: bool,
) -> int:
  if dry_run:
    return 0
  if processors is None:
    raise RuntimeError("processors are required unless dry_run is set")
  texts = [item.content for item in items]
  embeddings = processors.embedder.embed_batch(texts)
  tags = [processors.tagger.tag(text) for text in texts]
  stats = store_items(conn, items, embeddings, tags)
  return stats.items_inserted


def get_adapter(source: str, cfg: dict) -> Adapter:
  if source == "imessage":
    return IMessageAdapter(Path(cfg["chat_db_path"]))
  if source == "signal":
    return SignalAdapter(
      signal_dir=Path(cfg.get("signal_dir", "~/Library/Application Support/Signal")),
      include_attachments=bool(cfg.get("include_attachments", True)),
    )
  if source == "email":
    raw_sources = list(cfg.get("sources", []))
    active = [
      s for s in raw_sources
      if isinstance(s, dict) and s.get("enabled", True)
    ]
    return EmailAdapter(
      sources=active,
      user_addresses=list(cfg.get("user_addresses", [])),
      skip_newsletters=bool(cfg.get("skip_newsletters", True)),
    )
  if source == "notes":
    from yaams.ingest.obsidian import DEFAULT_SKIP_DIRS as _DEFAULT_SKIP_DIRS
    vault_path = cfg.get("vault_path")
    if not vault_path:
      raise ValueError(
        "notes source requires ingest.notes.vault_path in config.yaml "
        "(set via `yaams sources` → press `a` on the notes row)"
      )
    skip_dirs = set(cfg.get("skip_dirs") or _DEFAULT_SKIP_DIRS)
    return ObsidianAdapter(
      vault_path=Path(vault_path),
      skip_dirs=skip_dirs,
    )
  if source == "folders":
    raw_paths = cfg.get("paths") or []
    active_paths: list[str] = []
    for entry in raw_paths:
      if isinstance(entry, str):
        active_paths.append(entry)
      elif isinstance(entry, dict):
        path = entry.get("path")
        if isinstance(path, str) and entry.get("enabled", True):
          active_paths.append(path)
    if not active_paths:
      raise ValueError(
        "folders source requires at least one enabled path under ingest.folders.paths"
      )
    kwargs: dict = {"folder_paths": [Path(p) for p in active_paths]}
    extensions = cfg.get("extensions")
    if extensions:
      kwargs["extensions"] = tuple(extensions)
    skip_dirs = cfg.get("skip_dirs")
    if skip_dirs:
      kwargs["skip_dirs"] = set(skip_dirs)
    return FolderAdapter(**kwargs)
  if source == "tier2_ledger":
    notes_path = cfg.get("notes_path")
    if not notes_path:
      raise ValueError(
        "tier2_ledger source requires ingest.tier2_ledger.notes_path in config.yaml"
      )
    index_path = cfg.get(
      "index_path", str(Path(notes_path) / "08_indices" / "note_index.json")
    )
    return LedgerNotesAdapter(
      notes_path=Path(notes_path),
      index_path=Path(index_path),
    )
  if source == "github":
    return GitHubAdapter(username=cfg.get("username", ""))
  if source.startswith("calendar_"):
    profile = source[len("calendar_"):]
    return CalendarAdapter(
      profile=profile,
      skip_free=bool(cfg.get("skip_free", True)),
    )
  if source.startswith("teams_"):
    profile = source[len("teams_"):]
    engine = (cfg.get("engine_overrides") or {}).get(profile, "graph")
    if engine == "chatsvc":
      # owa-piggy mints an ic3.teams.office.com-audience token via FOCI
      # without re-auth; that audience is not gated by the same CA policy
      # as Graph /me/chats on tenants like SoftwareOne.
      token_source = OwaPiggyTokenSource(
        profile,
        command=["owa-piggy", "--profile", profile, "--audience", "ic3"],
      )
      client = ChatsvcClient(token_source)
      region = (cfg.get("chatsvc_region") or {}).get(profile, "emea")
      return ChatsvcAdapter(
        profile=profile,
        client=client,
        region=region,
        skip_bots=bool(cfg.get("skip_bots", True)),
        page_size=int(cfg.get("page_size", 50)),
      )
    token_source = OwaPiggyTokenSource(profile)
    graph = GraphClient(token_source)
    return TeamsAdapter(
      profile=profile,
      graph_client=graph,
      skip_bots=bool(cfg.get("skip_bots", True)),
      page_size=int(cfg.get("page_size", 50)),
    )
  if source.startswith("mail_"):
    profile = source[len("mail_"):]
    folders = tuple(cfg.get("folders") or ("Inbox", "SentItems"))
    return M365MailAdapter(
      profile=profile,
      folders=folders,
      user_addresses=list(cfg.get("user_addresses", [])),
      skip_newsletters=bool(cfg.get("skip_newsletters", True)),
      chunk_days=int(cfg.get("chunk_days", 30)),
    )
  raise ValueError(f"Unknown source: {source}")


def _record_ingest_run(
  conn,
  *,
  run_id: str,
  source: str,
  started_at: datetime,
  ended_at: datetime,
  duration_ms: float,
  seen: int,
  new: int,
  skipped: int,
  status: str,
  error: str | None,
) -> None:
  conn.execute(
    """
    INSERT INTO ingest_runs (
      run_id, source, started_at, ended_at, duration_ms,
      items_seen, items_new, items_skipped, status, error
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      run_id,
      source,
      started_at.isoformat(),
      ended_at.isoformat(),
      duration_ms,
      seen,
      new,
      skipped,
      status,
      error,
    ),
  )


def print_stats(
  conn,
  db_path: Path,
  run_stats: dict[str, dict[str, object]],
  *,
  dry_run: bool,
  total_duration_ms: float | None = None,
) -> None:
  stats = database_stats(conn)
  if dry_run:
    prefix = "Dry run complete."
  elif run_stats:
    prefix = "Ingest complete."
  else:
    prefix = "Database stats."
  click.echo(prefix)
  _print_sources(run_stats)
  totals = _print_run_table(run_stats, dry_run=dry_run)

  click.echo(f"  Total new items ingested: {totals['new']:,}")
  if totals["skipped"]:
    click.echo(f"  Total skipped: {totals['skipped']:,}")
  click.echo(f"  Total in DB: {stats['total']:,} items")
  click.echo(f"  Date range: {_date(stats['date_min'])} to {_date(stats['date_max'])}")
  click.echo(
    f"  Entities in DB: {stats['entities']:,} unique, "
    f"{stats['entity_links']:,} links"
  )
  if db_path.exists():
    click.echo(f"  Storage: {_size_mb(db_path):.1f} MB")
  if total_duration_ms is not None:
    click.echo(f"  Total elapsed: {_format_duration(total_duration_ms)}")


def _print_run_table(
  run_stats: dict[str, dict[str, object]],
  *,
  dry_run: bool,
) -> dict[str, int]:
  """Print the per-source table and return totals across non-failed rows."""
  if not run_stats:
    return {"seen": 0, "new": 0, "skipped": 0}

  ordered = _ordered_sources(run_stats)
  rows: list[dict[str, str]] = []
  diagnostics: list[tuple[str, dict]] = []
  totals = {"seen": 0, "new": 0, "skipped": 0}

  for source in ordered:
    s = run_stats[source]
    failure = s.get("failed")
    duration_ms = s.get("duration_ms")
    time_str = _format_duration(float(duration_ms)) if isinstance(duration_ms, (int, float)) else ""
    if failure:
      rows.append({
        "source": source, "items": "-", "new": "-", "skipped": "-",
        "time": time_str, "rate": f"FAILED: {failure}",
      })
      continue
    seen = int(s.get("seen", 0) or 0)
    new = int(s.get("new", 0) or 0)
    skipped = int(s.get("skipped", 0) or 0)
    items_count = int(s.get("files_walked", 0) or 0) if source == "folders" else seen
    totals["seen"] += items_count
    totals["new"] += new
    totals["skipped"] += skipped
    rate_str = ""
    if isinstance(duration_ms, (int, float)) and duration_ms > 0 and items_count > 0:
      rate_str = f"{items_count / (duration_ms / 1000):,.1f}/s"
    rows.append({
      "source": source,
      "items": f"{items_count:,}",
      "new": "-" if dry_run else f"{new:,}",
      "skipped": f"{skipped:,}" if skipped else "",
      "time": time_str,
      "rate": rate_str,
    })
    diagnostics.append((source, s))

  totals_row = {
    "source": "TOTAL",
    "items": f"{totals['seen']:,}",
    "new": "-" if dry_run else f"{totals['new']:,}",
    "skipped": f"{totals['skipped']:,}" if totals["skipped"] else "",
    "time": "",
    "rate": "",
  }

  columns = [
    ("source", "Source", "left"),
    ("items", "Items", "right"),
    ("new", "New", "right"),
    ("skipped", "Skipped", "right"),
    ("time", "Time", "right"),
    ("rate", "Rate", "right"),
  ]
  widths = {}
  for key, header, _ in columns:
    widths[key] = max(
      len(header),
      max((len(r.get(key, "")) for r in rows + [totals_row]), default=0),
    )

  def fmt_row(r: dict[str, str]) -> str:
    parts = []
    for key, _, align in columns:
      val = r.get(key, "")
      if align == "right":
        parts.append(val.rjust(widths[key]))
      else:
        parts.append(val.ljust(widths[key]))
    return "  " + "  ".join(parts).rstrip()

  click.echo("")
  click.echo(fmt_row({k: h for k, h, _ in columns}))
  separator = "  " + "  ".join("-" * widths[k] for k, _, _ in columns)
  click.echo(separator)
  for r in rows:
    click.echo(fmt_row(r))
  click.echo(separator)
  click.echo(fmt_row(totals_row))
  click.echo("")

  for source, s in diagnostics:
    _print_source_diagnostics(source, s)
  return totals


def _effective_since(conn, source: str, cfg: dict) -> datetime:
  configured = parse_iso_datetime(cfg["ingest"]["since"])
  watermark = get_watermark(conn, source)
  floor = datetime.min.replace(tzinfo=UTC)
  return max(configured, watermark or floor)


def _sources_to_run(source: str, cfg: dict | None = None) -> list[str]:
  cfg = cfg or {}
  teams_profiles = list((cfg.get("ingest", {}).get("teams", {}) or {}).get("profiles", []))
  teams_sources = [f"teams_{p}" for p in teams_profiles if _teams_profile_active(p)]
  cal_profiles = list((cfg.get("ingest", {}).get("calendar", {}) or {}).get("profiles", []))
  cal_sources = [f"calendar_{p}" for p in cal_profiles]
  mail_profiles = list((cfg.get("ingest", {}).get("mail", {}) or {}).get("profiles", []))
  mail_sources = [f"mail_{p}" for p in mail_profiles]
  if source == "all":
    return [
      "imessage", "signal", "email", "notes", "folders", "tier2_ledger",
      "github", *teams_sources, *cal_sources, *mail_sources,
    ]
  if source == "teams":
    return teams_sources
  if source == "calendar":
    return cal_sources
  if source == "mail":
    return mail_sources
  return [source]


def _teams_profile_active(profile: str) -> bool:
  profiles = sources_mod.discover_teams_profiles()
  if not profiles:
    return True
  by_alias = {str(p.get("alias")): p for p in profiles if p.get("alias")}
  discovered = by_alias.get(profile)
  if discovered is None:
    return False
  return bool(discovered.get("enabled", True))


def _config_section(source: str) -> str:
  if source.startswith("teams_") or source == "teams":
    return "teams"
  if source.startswith("calendar_") or source == "calendar":
    return "calendar"
  if source.startswith("mail_") or source == "mail":
    return "mail"
  return source


def _source_enabled(cfg: dict, source: str) -> bool:
  section = _config_section(source)
  if source.startswith("teams_") and not _teams_profile_active(source[len("teams_"):]):
    return False
  return bool(cfg.get("ingest", {}).get(section, {}).get("enabled", False))


def _source_paths(source: str, cfg: dict) -> list[str]:
  section = _config_section(source)
  source_cfg = cfg.get("ingest", {}).get(section, {})
  if source == "imessage":
    path = source_cfg.get("chat_db_path")
    return [f"chat.db: {Path(path).expanduser()}" if path else "chat.db: n/a"]
  if source == "signal":
    path = source_cfg.get("signal_dir", "~/Library/Application Support/Signal")
    return [f"signal: {Path(path).expanduser()}"]
  if source == "email":
    paths = []
    for entry in source_cfg.get("sources", []):
      source_type = entry.get("type", "unknown")
      path = entry.get("path", "n/a")
      paths.append(f"{source_type}: {Path(path).expanduser()}")
    return paths or ["n/a"]
  if source == "notes":
    path = source_cfg.get("vault_path")
    return [f"vault: {Path(path).expanduser()}" if path else "vault: n/a"]
  if source == "folders":
    paths = source_cfg.get("paths") or []
    return [f"folder: {Path(p).expanduser()}" for p in paths] or ["folders: n/a"]
  if source == "tier2_ledger":
    path = source_cfg.get("notes_path")
    return [f"ledger: {Path(path).expanduser()}" if path else "ledger: n/a"]
  if source == "github":
    return [f"github: {source_cfg.get('username', 'unknown')} (events)"]
  if source.startswith("calendar_"):
    profile = source[len("calendar_"):]
    return [f"owa-cal profile: {profile}"]
  if source.startswith("teams_"):
    profile = source[len("teams_"):]
    teams_cfg = cfg.get("ingest", {}).get("teams", {}) or {}
    engine = (teams_cfg.get("engine_overrides") or {}).get(profile, "graph")
    if engine == "chatsvc":
      region = (teams_cfg.get("chatsvc_region") or {}).get(profile, "emea")
      return [f"chatsvc {region} (owa-piggy profile): {profile}"]
    return [f"graph (owa-piggy profile): {profile}"]
  if source.startswith("mail_"):
    profile = source[len("mail_"):]
    folders = source_cfg.get("folders") or ["Inbox", "SentItems"]
    return [f"owa-mail profile: {profile} ({', '.join(folders)})"]
  return ["n/a"]


def _ordered_sources(run_stats: dict[str, dict[str, object]]) -> list[str]:
  fixed = [s for s in ("imessage", "email") if s in run_stats]
  teams = sorted(s for s in run_stats if s.startswith("teams_"))
  others = sorted(
    s for s in run_stats if s not in fixed and not s.startswith("teams_")
  )
  return fixed + teams + others


def _print_sources(run_stats: dict[str, dict[str, object]]) -> None:
  if not run_stats:
    return
  click.echo("  Sources:")
  for source in _ordered_sources(run_stats):
    since = run_stats[source].get("since", "n/a")
    paths = run_stats[source].get("paths", [])
    click.echo(f"    {source} since {since}:")
    for path in paths:
      click.echo(f"      - {path}")


def _print_source_diagnostics(source: str, stats: dict[str, object]) -> None:
  if source == "imessage":
    decoded = int(stats.get("decoded_attributed_body", 0))
    skipped = int(stats.get("skipped_attributed_body", 0))
    if decoded or skipped:
      click.echo(f"    attributedBody: {decoded:,} decoded, {skipped:,} skipped")
  if source == "email":
    skipped_emlx = int(stats.get("skipped_emlx", 0))
    skipped_dates = int(stats.get("skipped_email_dates", 0))
    skipped_news = int(stats.get("skipped_newsletters", 0))
    if skipped_emlx or skipped_dates or skipped_news:
      click.echo(
        f"    skipped email details: {skipped_emlx:,} parse errors, "
        f"{skipped_dates:,} invalid dates, "
        f"{skipped_news:,} newsletters/automated"
      )
  if source.startswith("teams_"):
    skipped_bots = int(stats.get("skipped_bots", 0))
    skipped_system = int(stats.get("skipped_system", 0))
    skipped_empty = int(stats.get("skipped_empty", 0))
    if skipped_bots or skipped_system or skipped_empty:
      click.echo(
        f"    skipped teams details: {skipped_bots:,} bots/automated, "
        f"{skipped_system:,} system events, {skipped_empty:,} empty/deleted"
      )
  if source.startswith("mail_"):
    skipped_news = int(stats.get("skipped_newsletters", 0))
    skipped_empty = int(stats.get("skipped_empty", 0))
    skipped_no_ts = int(stats.get("skipped_no_timestamp", 0))
    if skipped_news or skipped_empty or skipped_no_ts:
      click.echo(
        f"    skipped mail details: {skipped_news:,} newsletters/automated, "
        f"{skipped_empty:,} empty/fetch-failed, "
        f"{skipped_no_ts:,} no timestamp"
      )
