from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import click

from yaams.config import get_db_path, load_config, expand_path
from yaams.consolidate import (
  Consolidation,
  SessionConfig,
  build_consolidations,
)
from yaams.retrieve import (
  HybridQueryConfig,
  filter_results_by_entities,
  parse_query,
  query as run_query,
  route as route_parsed,
)
from yaams.signals import log_feedback, log_query, new_query_id, recent_queries
from yaams.synthesize import (
  build_synthesis_prompt,
  llm_adapter_from_config,
  synthesize_answer,
)
from yaams.db import open_db
from yaams.enrich import Embedder, EntityTagger
from yaams.ingest import Adapter, Item
from yaams.ingest.calendar import CalendarAdapter
from yaams.ingest.email_mbox import EmailAdapter
from yaams.ingest.github import GitHubAdapter
from yaams.ingest.imessage import IMessageAdapter
from yaams.ingest.ledger_notes import LedgerNotesAdapter
from yaams.ingest.obsidian import ObsidianAdapter
from yaams.ingest.signal import SignalAdapter
from yaams.ingest.teams import GraphClient, OwaPiggyTokenSource, TeamsAdapter
from yaams.schema import DEFAULT_EMBEDDING_DIM, init_schema
from yaams.store import (
  backfill_entity_sources,
  clear_consolidations,
  consolidation_stats,
  database_stats,
  fetch_items_for_consolidation,
  seed_entities,
  store_consolidations,
  store_items,
)
from yaams.time import format_local, parse_iso_datetime, to_local
from yaams.watermark import get_watermark, update_watermark


@click.group()
def cli() -> None:
  pass


@cli.command("init-db")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
@click.option("--require-vec", is_flag=True)
def init_db(config_path: str, require_vec: bool) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, require_vec=require_vec)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    seed_entities(conn, _entity_dictionary(cfg))
    backfill_entity_sources(conn, _entity_dictionary(cfg))
  finally:
    conn.close()
  click.echo(f"Initialized database: {db_path}")


@cli.command("ingest")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
@click.option(
  "--source",
  default="all",
  show_default=True,
  help=(
    "all, imessage, signal, email, notes, tier2_ledger, github, "
    "teams or teams_<profile>, calendar or calendar_<profile>"
  ),
)
@click.option("--dry-run", is_flag=True)
@click.option("--batch-size", default=64, show_default=True)
@click.option("--require-vec", is_flag=True)
def ingest(
  config_path: str,
  source: str,
  dry_run: bool,
  batch_size: int,
  require_vec: bool,
) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, require_vec=require_vec)
  run_stats: dict[str, dict[str, object]] = defaultdict(
    lambda: {"seen": 0, "new": 0, "skipped": 0}
  )
  run_id = uuid.uuid4().hex
  total_start = time.perf_counter()
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    seed_entities(conn, _entity_dictionary(cfg))
    backfill_entity_sources(conn, _entity_dictionary(cfg))
    processors = None if dry_run else ProcessingContext(cfg)
    for src in _sources_to_run(source, cfg):
      if not _source_enabled(cfg, src):
        continue
      src_started_at = datetime.now(UTC)
      src_perf_start = time.perf_counter()
      try:
        adapter = get_adapter(src, cfg["ingest"][_config_section(src)])
        source_stats = ingest_source(
          conn,
          src,
          adapter,
          cfg,
          batch_size=batch_size,
          dry_run=dry_run,
          processors=processors,
        )
      except Exception as exc:
        duration_ms = (time.perf_counter() - src_perf_start) * 1000
        error_text = f"{type(exc).__name__}: {exc}"
        click.echo(f"  {src}: failed - {error_text}", err=True)
        run_stats[src]["failed"] = error_text
        run_stats[src]["paths"] = _source_paths(src, cfg)
        run_stats[src]["duration_ms"] = duration_ms
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
      run_stats[src]["skipped_emlx"] = source_stats["skipped_emlx"]
      run_stats[src]["skipped_email_dates"] = source_stats["skipped_email_dates"]
      run_stats[src]["skipped_newsletters"] = source_stats.get("skipped_newsletters", 0)
      run_stats[src]["skipped_bots"] = source_stats.get("skipped_bots", 0)
      run_stats[src]["skipped_system"] = source_stats.get("skipped_system", 0)
      run_stats[src]["skipped_empty"] = source_stats.get("skipped_empty", 0)
      run_stats[src]["decoded_attributed_body"] = source_stats[
        "decoded_attributed_body"
      ]
      run_stats[src]["skipped_attributed_body"] = source_stats[
        "skipped_attributed_body"
      ]
      run_stats[src]["since"] = source_stats["since"]
      run_stats[src]["paths"] = _source_paths(src, cfg)
      run_stats[src]["duration_ms"] = source_stats["duration_ms"]
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
    print_stats(
      conn,
      db_path,
      run_stats,
      dry_run=dry_run,
      total_duration_ms=total_duration_ms,
    )
  finally:
    conn.close()


@cli.command("stats")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
def stats(config_path: str) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    print_stats(conn, db_path, {}, dry_run=False)
  finally:
    conn.close()


@cli.command("reset-db")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
@click.option("--yes", is_flag=True)
def reset_db(config_path: str, yes: bool) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  if not yes:
    click.confirm(f"Delete database at {db_path}?", abort=True)
  if db_path.exists():
    db_path.unlink()
  click.echo(f"Removed database: {db_path}")


@cli.command("query")
@click.argument("text", nargs=-1, required=True)
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
@click.option("--top-k", default=10, show_default=True, type=int)
@click.option(
  "--source",
  "source_filter",
  multiple=True,
  help="Filter to specific source(s); repeat for multiple (e.g. --source imessage --source teams_swon)",
)
@click.option("--since", default=None, help="ISO timestamp lower bound, e.g. 2026-01-01")
@click.option("--until", default=None, help="ISO timestamp upper bound")
@click.option(
  "--no-vector",
  is_flag=True,
  help="Skip dense vector search; FTS-only (faster, no embedder load)",
)
@click.option(
  "--no-consolidations",
  is_flag=True,
  help="Search raw items only (skip session consolidations)",
)
@click.option(
  "--format",
  "output_format",
  type=click.Choice(["text", "json"]),
  default="text",
  show_default=True,
)
@click.option("--answer/--no-answer", default=False, help="Synthesize a grounded answer with citations using the configured LLM backend")
@click.option("--no-log", is_flag=True, help="Skip signal logging for this query (default is to log)")
@click.option("--no-parse", is_flag=True, help="Skip the LLM query parser (raw text -> hybrid retrieve)")
@click.option("--explain", is_flag=True, help="Print the parsed query JSON before results")
@click.option("--high-quality", is_flag=True, help="Force synthesis-grade depth (bumps top_k, future rerank hook)")
def query_cmd(
  text: tuple[str, ...],
  config_path: str,
  top_k: int,
  source_filter: tuple[str, ...],
  since: str | None,
  until: str | None,
  no_vector: bool,
  no_consolidations: bool,
  output_format: str,
  answer: bool,
  no_log: bool,
  no_parse: bool,
  explain: bool,
  high_quality: bool,
) -> None:
  import time as _time

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  query_text = " ".join(text).strip()
  if not query_text:
    click.echo("Empty query.")
    return

  parsed = None
  parser_fallback_used = False
  if not no_parse:
    try:
      adapter_for_parse = llm_adapter_from_config(cfg)
      conn_parse = open_db(db_path, readonly=True)
      try:
        parsed = parse_query(query_text, adapter_for_parse, conn_parse)
      finally:
        conn_parse.close()
      parser_fallback_used = parsed.fallback_used
    except Exception as exc:
      click.echo(f"warning: parser unavailable, falling back to raw text ({exc})", err=True)
      parsed = None
      parser_fallback_used = True

  if high_quality and parsed is not None:
    parsed.high_quality = True

  if explain and parsed is not None:
    click.echo(f"parsed: {parsed.to_json()}")

  retrieve_start = _time.perf_counter()
  conn_ro = open_db(db_path, readonly=True)
  try:
    embedding = None
    if not no_vector:
      embedder = Embedder(**_embed_config(cfg))
      embedding = embedder.embed_batch([query_text])[0]

    base_cfg = HybridQueryConfig(
      top_k=top_k,
      source_filter=list(source_filter) or None,
      since=parse_iso_datetime(since) if since else None,
      until=parse_iso_datetime(until) if until else None,
      include_consolidations=not no_consolidations,
    )
    if parsed is not None:
      qcfg = route_parsed(
        parsed,
        base_cfg,
        explicit_since=since is not None,
        explicit_until=until is not None,
      )
    else:
      qcfg = base_cfg
    if high_quality:
      qcfg.high_quality = True
    fts_text = query_text
    if parsed is not None and parsed.topic_terms:
      fts_text = " ".join(parsed.topic_terms)
    results = run_query(conn_ro, fts_text, embedding=embedding, config=qcfg)
    if parsed is not None and qcfg.entity_filter:
      results = filter_results_by_entities(results, conn_ro, qcfg.entity_filter)
  finally:
    conn_ro.close()
  retrieval_ms = (_time.perf_counter() - retrieve_start) * 1000

  answer_result = None
  synthesis_ms = None
  if answer and results:
    synth_start = _time.perf_counter()
    try:
      adapter = llm_adapter_from_config(cfg)
      answer_result = synthesize_answer(query_text, results, adapter)
    except Exception as exc:
      click.echo(f"warning: synthesis backend failed: {exc}", err=True)
      answer_result = None
    synthesis_ms = (_time.perf_counter() - synth_start) * 1000

  query_id = new_query_id()
  if not no_log:
    conn_rw = open_db(db_path)
    try:
      init_schema(conn_rw, embedding_dim=_embedding_dim(cfg))
      log_query(
        conn_rw,
        query_id=query_id,
        text=query_text,
        top_k=top_k,
        source_filter=list(source_filter) or None,
        since=since,
        until=until,
        results=results,
        cited_result_ids=answer_result.cited_result_ids if answer_result else (),
        answer=answer_result.answer if answer_result else None,
        backend=answer_result.backend if answer_result else None,
        model=answer_result.model if answer_result else None,
        latency_ms=retrieval_ms + (synthesis_ms or 0),
        retrieval_ms=retrieval_ms,
        synthesis_ms=synthesis_ms,
        parsed_query=parsed.to_json() if parsed is not None else None,
        shape=parsed.shape if parsed is not None else None,
        confidence=answer_result.confidence if answer_result else None,
        confidence_reason=answer_result.confidence_reason if answer_result else None,
        gaps=answer_result.gaps if answer_result else None,
        parser_fallback=parser_fallback_used,
      )
    finally:
      conn_rw.close()

  if output_format == "json":
    import json as _json

    payload = {
      "query_id": query_id,
      "question": query_text,
      "retrieval_ms": round(retrieval_ms, 1),
      "synthesis_ms": round(synthesis_ms, 1) if synthesis_ms is not None else None,
      "results": [_result_to_dict(r) for r in results],
    }
    if parsed is not None:
      payload["parsed"] = _json.loads(parsed.to_json())
    if answer_result:
      payload["answer"] = answer_result.answer
      payload["answer_body"] = answer_result.answer_body
      payload["cited_ranks"] = answer_result.cited_ranks
      payload["cited_result_ids"] = answer_result.cited_result_ids
      payload["confidence"] = answer_result.confidence
      payload["confidence_reason"] = answer_result.confidence_reason
      payload["gaps"] = answer_result.gaps
      payload["backend"] = answer_result.backend
      payload["model"] = answer_result.model
    click.echo(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return

  if not results:
    click.echo("No results.")
    return

  if answer_result:
    click.echo(f"Answer ({answer_result.backend}{':' + answer_result.model if answer_result.model else ''}):")
    click.echo()
    click.echo(answer_result.answer_body or answer_result.answer)
    click.echo()
    if answer_result.confidence != "unknown":
      reason = f" - {answer_result.confidence_reason}" if answer_result.confidence_reason else ""
      click.echo(f"Confidence: {answer_result.confidence}{reason}")
    if answer_result.gaps:
      click.echo("Gaps:")
      for gap in answer_result.gaps:
        click.echo(f"  - {gap}")
    if answer_result.cited_ranks:
      click.echo(f"Cited: {answer_result.cited_ranks}")
    click.echo()

  click.echo(f"Top {len(results)} results for: {query_text!r}  (query_id={query_id})")
  click.echo()
  for i, r in enumerate(results, 1):
    _render_result(i, r)


@cli.command("feedback")
@click.argument("query_id")
@click.argument("kind", type=click.Choice(["hit", "miss", "correction", "note"]))
@click.option("--result", "result_id", default=None, help="Result id this feedback targets (omit for query-level)")
@click.option("--message", "-m", default=None, help="Free-text payload (e.g. \"expected X\" or correction details)")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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


def _result_to_dict(r) -> dict:
  return {
    "id": r.id,
    "kind": r.kind,
    "source": r.source,
    "timestamp": to_local(r.timestamp).isoformat() if hasattr(r.timestamp, "isoformat") else str(r.timestamp),
    "sender": r.sender,
    "subject": r.subject,
    "thread_id": r.thread_id,
    "score": round(r.score, 4),
    "item_count": r.item_count,
    "participants": r.participants,
    "content_preview": (r.content or "")[:400],
  }


def _render_result(rank: int, r) -> None:
  ts = format_local(r.timestamp, "%Y-%m-%d %H:%M %Z") if hasattr(r.timestamp, "strftime") else str(r.timestamp)
  kind_tag = "C" if r.kind == "consolidation" else "i"
  click.echo(f"[{rank:>2}] [{kind_tag}] {r.source:<14} {ts}  score={r.score:.3f}")
  if r.kind == "consolidation":
    click.echo(f"     {len(r.participants)} participants, {r.item_count} items: {', '.join(r.participants[:5])}")
  else:
    click.echo(f"     from: {r.sender}")
  preview = (r.content or "").strip().replace("\n", " ")
  if len(preview) > 240:
    preview = preview[:237] + "..."
  click.echo(f"     {preview}")
  click.echo()


@cli.command("consolidate")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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


def ingest_source(
  conn,
  source: str,
  adapter: Adapter,
  cfg: dict,
  *,
  batch_size: int,
  dry_run: bool,
  processors,
) -> dict[str, object]:
  started_at = datetime.now(UTC)
  perf_start = time.perf_counter()
  since = _effective_since(conn, source, cfg)
  batch: list[Item] = []
  latest_ts = since
  seen = 0
  inserted = 0
  iterator = _progress(adapter.extract(since), desc=f"Ingesting {source}")
  for item in iterator:
    seen += 1
    batch.append(item)
    if item.timestamp > latest_ts:
      latest_ts = item.timestamp
    if len(batch) >= batch_size:
      inserted += process_batch(conn, batch, processors, dry_run=dry_run)
      batch = []
  if batch:
    inserted += process_batch(conn, batch, processors, dry_run=dry_run)
  if not dry_run:
    update_watermark(conn, source, latest_ts)
    conn.commit()
  duration_ms = (time.perf_counter() - perf_start) * 1000
  return {
    "seen": seen,
    "new": inserted,
    "skipped": int(getattr(adapter, "skipped_emlx", 0))
    + int(getattr(adapter, "skipped_email_dates", 0))
    + int(getattr(adapter, "skipped_newsletters", 0))
    + int(getattr(adapter, "skipped_bots", 0))
    + int(getattr(adapter, "skipped_system", 0))
    + int(getattr(adapter, "skipped_empty", 0)),
    "skipped_emlx": int(getattr(adapter, "skipped_emlx", 0)),
    "skipped_email_dates": int(getattr(adapter, "skipped_email_dates", 0)),
    "skipped_newsletters": int(getattr(adapter, "skipped_newsletters", 0)),
    "skipped_bots": int(getattr(adapter, "skipped_bots", 0)),
    "skipped_system": int(getattr(adapter, "skipped_system", 0)),
    "skipped_empty": int(getattr(adapter, "skipped_empty", 0)),
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
    return EmailAdapter(
      sources=list(cfg.get("sources", [])),
      user_addresses=list(cfg.get("user_addresses", [])),
      skip_newsletters=bool(cfg.get("skip_newsletters", True)),
    )
  if source == "notes":
    from yaams.ingest.obsidian import DEFAULT_SKIP_DIRS as _DEFAULT_SKIP_DIRS
    skip_dirs = set(cfg.get("skip_dirs") or _DEFAULT_SKIP_DIRS)
    return ObsidianAdapter(
      vault_path=Path(cfg["vault_path"]),
      skip_dirs=skip_dirs,
    )
  if source == "tier2_ledger":
    notes_path = cfg.get("notes_path", "~/yaams/ledger-inbox")
    index_path = cfg.get("index_path", "~/yaams/ledger-inbox/08_indices/note_index.json")
    return LedgerNotesAdapter(
      notes_path=Path(notes_path),
      index_path=Path(index_path),
    )
  if source == "github":
    return GitHubAdapter(
      username=cfg.get("username", ""),
      include_private=bool(cfg.get("include_private", True)),
      include_forks=bool(cfg.get("include_forks", False)),
      fetch_issues=bool(cfg.get("fetch_issues", True)),
      fetch_prs=bool(cfg.get("fetch_prs", True)),
    )
  if source.startswith("calendar_"):
    profile = source[len("calendar_"):]
    return CalendarAdapter(
      profile=profile,
      skip_free=bool(cfg.get("skip_free", True)),
    )
  if source.startswith("teams_"):
    profile = source[len("teams_"):]
    token_source = OwaPiggyTokenSource(profile)
    graph = GraphClient(token_source)
    return TeamsAdapter(
      profile=profile,
      graph_client=graph,
      skip_bots=bool(cfg.get("skip_bots", True)),
      page_size=int(cfg.get("page_size", 50)),
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


def _format_duration(ms: float) -> str:
  if ms < 1000:
    return f"{ms:.0f}ms"
  seconds = ms / 1000
  if seconds < 60:
    return f"{seconds:.1f}s"
  minutes, seconds = divmod(seconds, 60)
  return f"{int(minutes)}m{seconds:04.1f}s"


def _format_throughput(seen: int, ms: float) -> str:
  if ms <= 0 or seen <= 0:
    return ""
  rate = seen / (ms / 1000)
  return f", {rate:,.1f} items/s"


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
  failed_sources = []
  for source in _ordered_sources(run_stats):
    failure = run_stats[source].get("failed")
    duration_ms = run_stats[source].get("duration_ms")
    timing = ""
    if isinstance(duration_ms, (int, float)):
      timing = f" [{_format_duration(float(duration_ms))}]"
    if failure:
      click.echo(f"  {source}: FAILED{timing} - {failure}")
      failed_sources.append(source)
      continue
    seen = run_stats[source]["seen"]
    new = run_stats[source]["new"]
    skipped = run_stats[source].get("skipped", 0)
    throughput = ""
    if isinstance(duration_ms, (int, float)):
      throughput = _format_throughput(int(seen), float(duration_ms))
    if dry_run:
      suffix = "would process, 0 written"
      if skipped:
        suffix = f"{suffix}, {skipped:,} skipped"
      click.echo(f"  {source}: {seen:,} items ({suffix}){timing}{throughput}")
    else:
      suffix = f"{new:,} new"
      if skipped:
        suffix = f"{suffix}, {skipped:,} skipped"
      click.echo(f"  {source}: {seen:,} items ({suffix}){timing}{throughput}")
    _print_source_diagnostics(source, run_stats[source])
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


def _effective_since(conn, source: str, cfg: dict) -> datetime:
  configured = parse_iso_datetime(cfg["ingest"]["since"])
  watermark = get_watermark(conn, source)
  floor = datetime.min.replace(tzinfo=UTC)
  return max(configured, watermark or floor)


def _sources_to_run(source: str, cfg: dict | None = None) -> list[str]:
  cfg = cfg or {}
  teams_profiles = list((cfg.get("ingest", {}).get("teams", {}) or {}).get("profiles", []))
  teams_sources = [f"teams_{p}" for p in teams_profiles]
  cal_profiles = list((cfg.get("ingest", {}).get("calendar", {}) or {}).get("profiles", []))
  cal_sources = [f"calendar_{p}" for p in cal_profiles]
  if source == "all":
    return ["imessage", "signal", "email", "notes", "tier2_ledger", "github", *teams_sources, *cal_sources]
  if source == "teams":
    return teams_sources
  if source == "calendar":
    return cal_sources
  return [source]


def _config_section(source: str) -> str:
  if source.startswith("teams_") or source == "teams":
    return "teams"
  if source.startswith("calendar_") or source == "calendar":
    return "calendar"
  return source


def _source_enabled(cfg: dict, source: str) -> bool:
  section = _config_section(source)
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
  if source == "tier2_ledger":
    path = source_cfg.get("notes_path")
    return [f"ledger: {Path(path).expanduser()}" if path else "ledger: n/a"]
  if source == "github":
    return [f"github: {source_cfg.get('username', 'unknown')} (issues + PRs)"]
  if source.startswith("calendar_"):
    profile = source[len("calendar_"):]
    return [f"owa-cal profile: {profile}"]
  if source.startswith("teams_"):
    profile = source[len("teams_"):]
    return [f"graph (owa-piggy profile): {profile}"]
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


def _embed_config(cfg: dict) -> dict:
  raw = dict(cfg.get("embed", {}))
  model = raw.pop("model")
  return {"model": model, **raw}


@dataclass
class ProcessingContext:
  cfg: dict
  _embedder: Embedder | None = field(default=None, init=False)
  _tagger: EntityTagger | None = field(default=None, init=False)

  @property
  def embedder(self) -> Embedder:
    if self._embedder is None:
      self._embedder = Embedder(**_embed_config(self.cfg))
    return self._embedder

  @property
  def tagger(self) -> EntityTagger:
    if self._tagger is None:
      ent_cfg = _entities_config(self.cfg)
      self._tagger = EntityTagger(
        ent_cfg.get("spacy_model"),
        _entity_dictionary(self.cfg),
        spacy_model_nb=ent_cfg.get("spacy_model_nb"),
      )
    return self._tagger


def _embedding_dim(cfg: dict) -> int:
  return int(cfg.get("embed", {}).get("dimension", DEFAULT_EMBEDDING_DIM))


def _entities_config(cfg: dict) -> dict:
  return dict(cfg.get("entities", {}))


def _entity_dictionary(cfg: dict) -> list[dict]:
  return list(_entities_config(cfg).get("dictionary", []))


def _progress(iterable: Iterable[Item], desc: str) -> Iterable[Item]:
  try:
    from tqdm import tqdm

    return tqdm(iterable, desc=desc)
  except ImportError:
    return iterable


def _date(value: str | None) -> str:
  if not value:
    return "n/a"
  return value[:10]


def _size_mb(path: Path) -> float:
  return path.stat().st_size / (1024 * 1024)


@cli.group("promote")
def promote_group() -> None:
  """Generate and review promotion candidates for the Tier 2 ledger."""
  pass


@promote_group.command("generate")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
@click.option("--all", "review_all", is_flag=True, help="Review all statuses, not just pending")
def promote_review(config_path: str, review_all: bool) -> None:
  from yaams.promote.candidates import (
    fetch_pending, mark_items_promoted, update_status,
  )
  from yaams.promote.review import format_note, render_candidate, write_to_inbox

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  promote_cfg_raw = cfg.get("promote", {}) or {}
  inbox_path = expand_path(
    promote_cfg_raw.get("inbox_path", "~/yaams/ledger-inbox/00_inbox")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--alias", "aliases", multiple=True, help="Repeatable: --alias JX --alias Jacob")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
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
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
def entities_manage(config_path: str) -> None:
  """Interactive entity dictionary manager."""

  def _show(cfg: dict, conn: "sqlite3.Connection") -> None:
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


@cli.group("enrich")
def enrich_group() -> None:
  """Re-enrich stored items (tags, embeddings)."""
  pass


@enrich_group.command("retag")
@click.option("--config", "config_path", default=None, help="Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, ~/.config/yaams/config.yaml, or repo root if omitted.")
@click.option("--source", default=None, help="Limit to a specific source (e.g. imessage).")
@click.option("--batch-size", default=500, show_default=True)
def enrich_retag(config_path: str, source: str | None, batch_size: int) -> None:
  """Re-tag all stored items with the current NER models and dictionary."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    ent_cfg = _entities_config(cfg)
    tagger = EntityTagger(
      ent_cfg.get("spacy_model"),
      _entity_dictionary(cfg),
      spacy_model_nb=ent_cfg.get("spacy_model_nb"),
    )
    where = "WHERE source = ?" if source else ""
    params: tuple = (source,) if source else ()
    total = conn.execute(
      f"SELECT count(*) FROM items {where}", params
    ).fetchone()[0]
    click.echo(f"Re-tagging {total} items{'  (source=' + source + ')' if source else ''}...")
    offset = 0
    updated = 0
    while offset < total:
      rows = conn.execute(
        f"SELECT id, content FROM items {where} ORDER BY id LIMIT ? OFFSET ?",
        (*params, batch_size, offset),
      ).fetchall()
      if not rows:
        break
      with conn:
        for row in rows:
          tags = tagger.tag(row["content"] or "")
          from yaams.store import _replace_entity_links
          _replace_entity_links(conn, row["id"], tags)
          updated += 1
      offset += batch_size
      click.echo(f"  {min(offset, total)}/{total}")
    click.echo(f"Done. Re-tagged {updated} items.")
  finally:
    conn.close()


if __name__ == "__main__":
  cli()
