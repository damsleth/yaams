"""FastMCP server wiring YAAMS Tier-1 verbs as MCP tools.

Ported from cognitive-ledger's ``ledger/mcp/server.py`` (plan 44). YAAMS is the
Tier-1 (raw digital-exhaust) engine; this server lets any MCP client query it
directly over stdio — superseding the subprocess ``yaams_query`` shim the
ledger MCP server used to wrap it.

Every tool response is routed through ``scrub_for_egress`` — a defense-in-depth
private-content gate — before leaving the process. Write tools are off unless
the server is started with ``allow_write=True`` (``yaams mcp --allow-write``).
"""

from __future__ import annotations

import re
import time as _time
from typing import Any

from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.retrieve import HybridQueryConfig, attach_trust_verdicts
from yaams.retrieve import query as run_query
from yaams.signals import log_query, new_query_id

_LEDGER_SOURCE_ID = "tier2_ledger"
_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def _require_mcp():
  try:
    from mcp.server.fastmcp import FastMCP
  except ImportError as exc:  # pragma: no cover - import guard
    raise RuntimeError(
      "The MCP server requires the optional 'mcp' package. "
      "Install it with: pip install 'yaams[mcp]'"
    ) from exc
  return FastMCP


def scrub_for_egress(obj: Any) -> Any:
  """Recursively strip ``<private>…</private>`` spans from any string in *obj*.

  Defense-in-depth: keeps fenced private content from leaving the process even
  if it ever lands in ingested item text.
  """
  if isinstance(obj, str):
    return _PRIVATE_RE.sub("", obj)
  if isinstance(obj, dict):
    return {k: scrub_for_egress(v) for k, v in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [scrub_for_egress(v) for v in obj]
  return obj


def _embed_config(cfg: dict) -> dict:
  # Reuse the CLI's embed-config resolution (model + models_dir fallback).
  from yaams.cli._shared import _embed_config as _shared_embed_config

  return _shared_embed_config(cfg)


def _trust_flags(cfg: dict) -> tuple[bool, bool]:
  raw = cfg.get("trust")
  trust = raw if isinstance(raw, dict) else {}
  return (
    bool(trust.get("show_trust_verdict", True)),
    bool(trust.get("provenance_weighting_enabled", False)),
  )


def _resolve_sources(tier: str, source: str) -> tuple[list[str] | None, bool]:
  """Return (source_filter, exclude_ledger) for a tier/source selection."""
  if source:
    return [source], False
  if tier == "ledger":
    return [_LEDGER_SOURCE_ID], False
  if tier == "raw":
    return None, True  # everything except the Tier-2 ledger
  return None, False  # both tiers


def _feedback_boost(cfg: dict) -> bool:
  """Read the P2 precision-with-use flag (retrieve.feedback_boost, default off)."""
  retrieve = cfg.get("retrieve")
  return bool(retrieve.get("feedback_boost")) if isinstance(retrieve, dict) else False


def _log_mcp_query(
  cfg: dict, *, query_id: str, text: str, top_k: int, source_filter: list[str] | None,
  results: list, **fields: Any,
) -> None:
  """Persist an MCP query to the signal log with ``provenance='mcp'``.

  The query itself runs on a readonly connection; logging needs a writable one,
  so this opens its own. Idempotent schema init keeps it safe on any live DB.
  """
  from yaams.cli._shared import _embedding_dim
  from yaams.schema import init_schema

  conn = open_db(get_db_path(cfg))
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    log_query(
      conn, query_id=query_id, text=text, top_k=top_k,
      source_filter=source_filter, since=None, until=None, results=results,
      provenance="mcp", **fields,
    )
  finally:
    conn.close()


def _run_text_query(cfg: dict, query_text: str, *, top_k: int, tier: str, source: str) -> list:
  """Shared retrieval path: embed -> hybrid query -> attach trust verdicts."""
  from yaams.enrich import Embedder

  db_path = get_db_path(cfg)
  source_filter, exclude_ledger = _resolve_sources(tier, source)
  embedder = Embedder(**_embed_config(cfg), quiet=True)
  embedding = embedder.embed_batch([query_text])[0]
  qcfg = HybridQueryConfig(
    top_k=top_k, source_filter=source_filter, feedback_boost=_feedback_boost(cfg)
  )
  conn = open_db(db_path, readonly=True)
  try:
    results = run_query(conn, query_text, embedding=embedding, config=qcfg)
    if exclude_ledger:
      results = [r for r in results if r.source != _LEDGER_SOURCE_ID]
    show_trust, weighting = _trust_flags(cfg)
    attach_trust_verdicts(
      results,
      conn,
      show_trust_verdict=show_trust,
      provenance_weighting_enabled=weighting,
    )
  finally:
    conn.close()
  return results


def _results_payload(results: list) -> dict:
  from yaams.cli.query import _result_to_dict

  return {"results": [_result_to_dict(r) for r in results]}


def create_server(*, config_path: str | None = None, allow_write: bool = False):
  """Build (but do not run) the FastMCP server with YAAMS tools."""
  FastMCP = _require_mcp()
  cfg = load_config(config_path)
  mcp = FastMCP("yaams")

  @mcp.tool()
  def yaams_query(query: str, limit: int = 10, tier: str = "both", source: str = "") -> dict:
    """Search Tier-1 digital exhaust. Returns ranked results with trust verdicts.

    tier: "both" (default), "raw" (exclude curated ledger), or "ledger" (only).
    source: restrict to a single ingest source (e.g. "email", "github").
    """
    query_id = new_query_id()
    t0 = _time.perf_counter()
    results = _run_text_query(cfg, query, top_k=limit, tier=tier, source=source)
    retrieval_ms = (_time.perf_counter() - t0) * 1000
    source_filter, _ = _resolve_sources(tier, source)
    _log_mcp_query(
      cfg, query_id=query_id, text=query, top_k=limit, source_filter=source_filter,
      results=results, latency_ms=retrieval_ms, retrieval_ms=retrieval_ms,
    )
    payload = _results_payload(results)
    payload["query_id"] = query_id
    return scrub_for_egress(payload)

  @mcp.tool()
  def yaams_answer(question: str, limit: int = 5, tier: str = "both") -> dict:
    """Synthesize a grounded, cited answer over Tier-1 results."""
    from yaams.synthesize import llm_adapter_from_config, synthesize_answer

    query_id = new_query_id()
    t0 = _time.perf_counter()
    results = _run_text_query(cfg, question, top_k=limit, tier=tier, source="")
    retrieval_ms = (_time.perf_counter() - t0) * 1000
    if not results:
      _log_mcp_query(
        cfg, query_id=query_id, text=question, top_k=limit, source_filter=None,
        results=[], latency_ms=retrieval_ms, retrieval_ms=retrieval_ms,
        confidence="unknown",
      )
      return {"answer": "", "confidence": "unknown", "results": [], "query_id": query_id}
    adapter = llm_adapter_from_config(cfg)
    t1 = _time.perf_counter()
    answer = synthesize_answer(question, results, adapter)
    synthesis_ms = (_time.perf_counter() - t1) * 1000
    # The cited results ARE the automatic positive label — this is what makes
    # the flywheel turn without any human in the loop.
    _log_mcp_query(
      cfg, query_id=query_id, text=question, top_k=limit, source_filter=None,
      results=results, cited_result_ids=answer.cited_result_ids,
      answer=answer.answer, backend=answer.backend, model=answer.model,
      confidence=answer.confidence, confidence_reason=answer.confidence_reason,
      gaps=answer.gaps, latency_ms=retrieval_ms + synthesis_ms,
      retrieval_ms=retrieval_ms, synthesis_ms=synthesis_ms,
    )
    payload = {
      "query_id": query_id,
      "answer": answer.answer_body or answer.answer,
      "confidence": answer.confidence,
      "confidence_reason": answer.confidence_reason,
      "gaps": answer.gaps,
      "cited_ranks": answer.cited_ranks,
      "cited_result_ids": answer.cited_result_ids,
      "backend": answer.backend,
      "model": answer.model,
      "results": _results_payload(results)["results"],
    }
    return scrub_for_egress(payload)

  if allow_write:

    @mcp.tool()
    def yaams_feedback(query_id: str, rank: int, verdict: str, note: str = "") -> dict:
      """Log a relevance signal against a prior query result (write-gated).

      verdict: one of hit | relevant | miss | correction | thin. ``rank`` is the
      1-based position from the query the feedback refers to.
      """
      from yaams.signals import log_feedback

      kind = (verdict or "").strip().lower()
      allowed = {"hit", "relevant", "miss", "correction", "thin"}
      if kind not in allowed:
        return {"logged": False, "error": f"verdict must be one of {sorted(allowed)}"}
      conn = open_db(get_db_path(cfg))
      try:
        # Resolve the result id for this rank so the signal is attributable
        # (drives trust verdicts); ranking-only verdicts (miss) may have none.
        row = conn.execute(
          "SELECT result_id FROM query_results WHERE query_id = ? AND rank = ?",
          (query_id, rank),
        ).fetchone()
        result_id = row[0] if row else None
        feedback_id = log_feedback(
          conn,
          query_id=query_id,
          kind=kind,
          result_id=result_id,
          payload={"rank": rank, "note": note} if note else {"rank": rank},
        )
      finally:
        conn.close()
      return {
        "logged": True,
        "feedback_id": feedback_id,
        "kind": kind,
        "result_id": result_id,
      }

  return mcp


def run(*, config_path: str | None = None, allow_write: bool = False) -> None:
  """Launch the MCP server over stdio (blocking)."""
  server = create_server(config_path=config_path, allow_write=allow_write)
  server.run()
