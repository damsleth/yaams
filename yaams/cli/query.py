from __future__ import annotations

import click

from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.enrich import Embedder
from yaams.retrieve import (
  HybridQueryConfig,
  filter_results_by_entities,
  parse_query,
  query as run_query,
  route as route_parsed,
)
from yaams.schema import init_schema
from yaams.signals import log_query, new_query_id
from yaams.synthesize import llm_adapter_from_config, synthesize_answer
from yaams.time import format_local, parse_iso_datetime, to_local

from yaams.cli._root import cli
from yaams.cli._shared import _embed_config, _embedding_dim, config_option


@cli.command("query")
@click.argument("text", nargs=-1, required=True)
@config_option
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
