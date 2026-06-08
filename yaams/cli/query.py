from __future__ import annotations

import os

import click

from yaams.cli._envelope import JsonFailureGuard
from yaams.cli._root import cli
from yaams.cli._shared import (
  _embed_config,
  _embedding_dim,
  _self_identities,
  config_option,
)
from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.enrich import Embedder
from yaams.render import (
  render_consolidation_snippet,
  short_participants,
  short_sender,
)
from yaams.retrieve import (
  HybridQueryConfig,
  filter_results_by_entities,
  parse_query,
)
from yaams.retrieve import (
  query as run_query,
)
from yaams.retrieve import (
  route as route_parsed,
)
from yaams.retrieve.associate import expand_query_entities
from yaams.retrieve.metadata import entities_matching
from yaams.schema import init_schema
from yaams.signals import log_query, new_query_id
from yaams.synthesize import llm_adapter_from_config, synthesize_answer
from yaams.time import format_local, parse_iso_datetime, to_local

_LEDGER_SOURCE_ID = "tier2_ledger"


def _cli_provenance() -> str | None:
  """Tag CLI-issued queries; flag hugr passthrough when detectable.

  ``hugr`` sets ``HUGR_PASSTHROUGH=1`` when shelling out to ``yaams query``
  so those rows can be distinguished from a direct ``yaams query`` call.
  Returns ``None`` under pytest so ``detect_provenance`` falls through to
  its ``PYTEST_CURRENT_TEST`` check and tags the row ``"test"`` instead.
  """
  if os.environ.get("PYTEST_CURRENT_TEST"):
    return None
  if os.environ.get("HUGR_PASSTHROUGH"):
    return "hugr"
  return "cli"


def _parse_meta_pairs(meta: tuple[str, ...]) -> dict[str, str]:
  """Parse --meta KEY=VALUE flags into a dict. Pairs without '=' or with an
  empty key are skipped (silently tolerant; the resolver AND-s what remains)."""
  out: dict[str, str] = {}
  for raw in meta:
    if "=" not in raw:
      continue
    key, value = raw.split("=", 1)
    if key.strip():
      out[key.strip()] = value
  return out


def _resolve_source_filter(
  source_filter: tuple[str, ...],
  tier: str | None,
) -> list[str] | None:
  """Translate --tier and --source aliases to canonical source ids.

  - `--source ledger` -> `tier2_ledger` (CLI alias from the spec)
  - `--tier raw` -> exclude `tier2_ledger` (every other configured source)
  - `--tier ledger` -> only `tier2_ledger`
  - `--tier both` (default) -> no tier filter
  - Explicit `--source` wins over `--tier` (caller asked specifically)
  """
  # Step 1: canonicalize any 'ledger' alias in --source.
  resolved = [
    _LEDGER_SOURCE_ID if s == "ledger" else s for s in source_filter
  ]
  if resolved:
    return resolved
  if not tier or tier == "both":
    return None
  if tier == "ledger":
    return [_LEDGER_SOURCE_ID]
  # tier == "raw": negative filter is expressed downstream; we cannot
  # cleanly express "everything except tier2_ledger" via the existing
  # source_filter list (which is a positive include list). For now,
  # return None and let route_parsed apply the exclusion. We mark the
  # config below so the route step can act on it.
  return None


@cli.command("query")
@click.argument("text", nargs=-1, required=True)
@config_option
@click.option("--top-k", default=10, show_default=True, type=int)
@click.option(
  "--source",
  "source_filter",
  multiple=True,
  help=(
    "Filter to specific source(s); repeat for multiple (e.g. --source "
    "imessage --source teams_swon). `--source ledger` is a CLI alias for "
    "the internal `tier2_ledger` source id."
  ),
)
@click.option(
  "--tier",
  type=click.Choice(["raw", "ledger", "both"]),
  default=None,
  help=(
    "Restrict the result tier: raw (everything except curated ledger "
    "notes), ledger (only tier2_ledger), or both. Default: both. "
    "Explicit --source wins over --tier."
  ),
)
@click.option("--since", default=None, help="ISO timestamp lower bound, e.g. 2026-01-01")
@click.option("--until", default=None, help="ISO timestamp upper bound")
@click.option(
  "--sort",
  "sort",
  type=click.Choice(["relevance", "newest", "oldest"]),
  default=None,
  help=(
    "Order results by relevance (default), newest-first, or oldest-first. "
    "Explicit --sort overrides the parser's shape-based inference."
  ),
)
@click.option(
  "--no-vector",
  is_flag=True,
  help="Skip dense vector search; FTS-only (faster, no embedder load)",
)
@click.option(
  "--no-synonyms",
  is_flag=True,
  help="Disable entity-alias synonym expansion of the FTS query "
       "(e.g. don't expand 'nc' to also match 'Norconsult')",
)
@click.option(
  "--assoc",
  is_flag=True,
  help="Widen entity-filtered results with associated entities "
       "(e.g. a query about 'fdep' also surfaces 'langkaia'), ranked below "
       "exact matches. Requires a resolved query entity; build edges with "
       "'yaams assoc build'.",
)
@click.option(
  "--tag",
  "tags",
  multiple=True,
  help="Restrict/boost to documents whose entities carry this tag "
       "(repeatable; entities must carry ALL given tags). "
       "Set tags with 'yaams entities tag'.",
)
@click.option(
  "--meta",
  "meta",
  multiple=True,
  help="Restrict/boost by entity attribute KEY=VALUE (repeatable; AND-ed). "
       "Set with 'yaams entities set'.",
)
@click.option(
  "--tag-mode",
  type=click.Choice(["filter", "boost"]),
  default="filter",
  show_default=True,
  help="filter: hard-restrict to matching-metadata entities. "
       "boost: keep all results but lift matching ones in ranking.",
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
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="Machine mode (alias for --format json). Reserved-key contract: success "
       "documents have no top-level `ok` field.",
)
@click.option(
  "--pretty",
  is_flag=True,
  help="Human-readable rendering (alias for --format text).",
)
@click.option("--answer/--no-answer", default=False, help="Synthesize a grounded answer with citations using the configured LLM backend")
@click.option("--no-log", is_flag=True, help="Skip signal logging for this query (default is to log)")
@click.option("--no-parse", is_flag=True, help="Skip the LLM query parser (raw text -> hybrid retrieve)")
@click.option("--explain", is_flag=True, help="Print the parsed query JSON before results")
@click.option("--high-quality", is_flag=True, help="Force synthesis-grade depth (bumps top_k, future rerank hook)")
@click.option(
  "--lang",
  "lang_filter",
  type=click.Choice(["no", "en"]),
  default=None,
  help="Restrict results to items in this language (no=Norwegian, en=English).",
)
@click.option(
  "--prompt/--no-prompt",
  "feedback_prompt",
  default=None,
  help="After results, ask for a feedback verdict (h/m/1-9/n). "
       "Default: on when stdin and stdout are TTYs and --format=text.",
)
def query_cmd(
  text: tuple[str, ...],
  config_path: str,
  top_k: int,
  source_filter: tuple[str, ...],
  tier: str | None,
  since: str | None,
  until: str | None,
  sort: str | None,
  no_vector: bool,
  no_synonyms: bool,
  assoc: bool,
  tags: tuple[str, ...],
  meta: tuple[str, ...],
  tag_mode: str,
  no_consolidations: bool,
  output_format: str,
  as_json: bool,
  pretty: bool,
  answer: bool,
  no_log: bool,
  no_parse: bool,
  explain: bool,
  high_quality: bool,
  lang_filter: str | None,
  feedback_prompt: bool | None,
) -> None:
  # --json and --pretty are aliases for --format. Last-one-wins by
  # presence; if both are set we honor --json (machine mode is the
  # safer default when there's ambiguity).
  if as_json:
    output_format = "json"
  elif pretty:
    output_format = "text"

  # Wrap the entire body so any failure (config load, db open, embedder
  # init, parser failure, ...) becomes a single-line JSON data_error
  # envelope on stdout under --json, instead of a traceback. Plan 06.
  with JsonFailureGuard("query", as_json=(output_format == "json")):
    resolved_sources = _resolve_source_filter(source_filter, tier)
    # `--tier raw` means "exclude tier2_ledger". We translate by mutating
    # the resolved list later if route_parsed didn't set it.
    tier_raw_exclude = (tier == "raw" and not source_filter)
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
        embedder = Embedder(**_embed_config(cfg), quiet=_quiet_embedder(output_format))
        embedding = embedder.embed_batch([query_text])[0]

      sort_map = {"newest": "desc", "oldest": "asc", "relevance": "relevance"}
      base_cfg = HybridQueryConfig(
        top_k=top_k,
        source_filter=list(resolved_sources) if resolved_sources else None,
        since=parse_iso_datetime(since) if since else None,
        until=parse_iso_datetime(until) if until else None,
        sort=sort_map[sort] if sort else "relevance",
        include_consolidations=not no_consolidations,
        expand_synonyms=not no_synonyms,
        lang_filter=lang_filter,
      )
      if parsed is not None:
        qcfg = route_parsed(
          parsed,
          base_cfg,
          explicit_since=since is not None,
          explicit_until=until is not None,
          explicit_sort=sort is not None,
          self_identities=_self_identities(cfg),
        )
      else:
        qcfg = base_cfg
      if high_quality:
        qcfg.high_quality = True
      if assoc and qcfg.entity_filter:
        # Widen the entity allowlist with associated entities and carry their
        # weights so associated-only documents surface but rank below exact
        # matches. Keeps the hard entity filter; just makes it fuzzy-aware.
        expanded, weights = expand_query_entities(conn_ro, qcfg.entity_filter)
        qcfg.entity_filter = expanded
        qcfg.assoc_weights = weights
      # Entity-metadata constraints (--tag / --meta) resolve to a set of
      # qualifying entities, then reuse the entity machinery: filter mode
      # restricts the allowlist, boost mode lifts matching docs in ranking.
      tag_filter_no_match = False
      if tags or meta:
        meta_dict = _parse_meta_pairs(meta)
        matched = entities_matching(conn_ro, tags=list(tags), meta=meta_dict)
        if tag_mode == "filter":
          if matched:
            qcfg.entity_filter = matched
          else:
            tag_filter_no_match = True
        elif matched:
          qcfg.boost_entities = matched

      fts_text = query_text
      if parsed is not None and parsed.topic_terms:
        fts_text = " ".join(parsed.topic_terms)
      if tag_filter_no_match:
        results = []  # no entity carries the requested metadata
      else:
        results = run_query(conn_ro, fts_text, embedding=embedding, config=qcfg)
      if not tag_filter_no_match and parsed is not None and qcfg.entity_filter:
        results = filter_results_by_entities(results, conn_ro, qcfg.entity_filter)
      if tier_raw_exclude:
        results = [r for r in results if r.source != _LEDGER_SOURCE_ID]
    finally:
      conn_ro.close()
    retrieval_ms = (_time.perf_counter() - retrieve_start) * 1000

    answer_result = None
    synthesis_ms = None
    if answer and results:
      synth_start = _time.perf_counter()
      try:
        adapter = llm_adapter_from_config(cfg)
        answer_result = synthesize_answer(
          query_text, results, adapter,
          shape=parsed.shape if parsed is not None else None,
        )
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
          source_filter=list(resolved_sources) if resolved_sources else None,
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
          provenance=_cli_provenance(),
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

    if not no_log and _should_prompt(feedback_prompt, output_format):
      _prompt_feedback(
        db_path=db_path,
        query_id=query_id,
        query_text=query_text,
        results=results,
        shape=parsed.shape if parsed is not None else None,
        parser_fallback=parser_fallback_used,
      )


def _quiet_embedder(output_format: str) -> bool:
  """Suppress sentence-transformers / HF progress bars unless the user
  asked for JSON (which already implies a non-interactive caller that may
  want stderr noise for debugging) or set `YAAMS_VERBOSE`."""
  if os.environ.get("YAAMS_VERBOSE"):
    return False
  return output_format == "text"


def _should_prompt(feedback_prompt: bool | None, output_format: str) -> bool:
  """Decide whether to show the inline feedback prompt.

  Explicit ``--prompt`` / ``--no-prompt`` always win. Otherwise prompt
  only in interactive text mode: both stdin and stdout must be TTYs and
  the output_format must be ``text``. JSON callers never see a prompt.
  """
  import sys

  if output_format != "text":
    return False
  if feedback_prompt is not None:
    return feedback_prompt
  return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_feedback(
  *,
  db_path,
  query_id: str,
  query_text: str,
  results,
  shape: str | None = None,
  parser_fallback: bool = False,
) -> None:
  """Inline post-query verdict prompt. One keystroke, no Enter required.

  Shape-gated: answer-shaped queries grade the answer (hit/miss/correction);
  recall-shaped queries — and ones the parser couldn't understand
  (``parser_fallback``) — grade the set's usefulness (relevant/thin), since
  there is no single 'right' result to point at.
  """
  from yaams.signals import (
    flush_session,
    is_answer_shaped,
    log_feedback,
    noise_cascade,
  )

  if not results:
    return

  answer_shaped = is_answer_shaped(shape, parser_fallback)
  click.echo("")
  if answer_shaped:
    prompt = "Useful? [h]it  [m]iss  [1-9]correction  [n]oise  [enter]skip"
  else:
    prompt = "Useful set? [r]elevant  [t]hin  [n]oise  [enter]skip"
  click.echo(prompt, nl=False)
  try:
    ch = click.getchar(echo=False)
  except (KeyboardInterrupt, EOFError):
    click.echo("")
    return
  click.echo("")  # finalize the prompt line

  # Treat enter / esc / space / any non-verdict input as skip.
  if ch in ("\r", "\n", " ", "\x1b", "\x03", ""):
    return

  try:
    conn = open_db(db_path)
  except Exception as exc:
    click.echo(f"feedback skipped (db open failed: {exc})", err=True)
    return

  try:
    # Noise applies to any shape and cascades to identical-text queries.
    if ch == "n":
      entries = noise_cascade(conn, query_id=query_id, text=query_text)
      written = flush_session(conn, entries)
      click.echo(f"  logged: noise (cascaded to {written} row(s))")
      return

    if answer_shaped:
      if ch == "h":
        log_feedback(
          conn, query_id=query_id, kind="hit", result_id=results[0].id
        )
        click.echo(f"  logged: hit on rank 1 ({results[0].id})")
        return
      if ch == "m":
        log_feedback(conn, query_id=query_id, kind="miss")
        click.echo("  logged: miss")
        return
      if len(ch) == 1 and ch in "123456789":
        rank = int(ch)
        if rank > len(results):
          click.echo(f"  no result at rank {rank}; skipped")
          return
        target = results[rank - 1]
        log_feedback(
          conn, query_id=query_id, kind="correction", result_id=target.id
        )
        click.echo(f"  logged: correction → rank {rank} ({target.id})")
        return
    else:
      if ch == "r":
        log_feedback(conn, query_id=query_id, kind="relevant")
        click.echo("  logged: relevant (useful set)")
        return
      if ch == "t":
        log_feedback(conn, query_id=query_id, kind="thin")
        click.echo("  logged: thin (not useful)")
        return

    click.echo(f"  {ch!r} — no verdict; skipped")
  finally:
    conn.close()


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


_BODY_WIDTH = 92
_BODY_INDENT = "     "


def _render_result(rank: int, r) -> None:
  import textwrap

  ts = (
    format_local(r.timestamp, "%Y-%m-%d %H:%M %Z")
    if hasattr(r.timestamp, "strftime")
    else str(r.timestamp)
  )
  click.echo(f"[{rank:>2}] {r.source} · {ts} · score {r.score:.3f}")

  if r.kind == "consolidation":
    parts = short_participants(r.participants or [])
    meta = f"{r.item_count} items"
    if parts:
      meta += f" · {parts}"
    click.echo(f"{_BODY_INDENT}{meta}")
    body = render_consolidation_snippet(
      r.content or "", multiline=True, max_chars=600
    )
  else:
    click.echo(f"{_BODY_INDENT}from {short_sender(r.sender or '')}")
    body = (r.content or "").strip()
    if len(body) > 600:
      body = body[:599].rstrip() + "…"

  for raw_line in body.splitlines() or [""]:
    wrapped = textwrap.wrap(
      raw_line, width=_BODY_WIDTH, subsequent_indent="  "
    ) or [""]
    for piece in wrapped:
      click.echo(f"{_BODY_INDENT}{piece}")
  click.echo()
