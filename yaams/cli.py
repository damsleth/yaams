from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import click

from yaams.config import get_db_path, load_config
from yaams.consolidate import (
  Consolidation,
  SessionConfig,
  build_consolidations,
)
from yaams.retrieve import HybridQueryConfig, query as run_query
from yaams.db import open_db
from yaams.enrich import Embedder, EntityTagger
from yaams.ingest import Adapter, Item
from yaams.ingest.email_mbox import EmailAdapter
from yaams.ingest.imessage import IMessageAdapter
from yaams.ingest.teams import GraphClient, OwaPiggyTokenSource, TeamsAdapter
from yaams.schema import DEFAULT_EMBEDDING_DIM, init_schema
from yaams.store import (
  clear_consolidations,
  consolidation_stats,
  database_stats,
  fetch_items_for_consolidation,
  seed_entities,
  store_consolidations,
  store_items,
)
from yaams.time import parse_iso_datetime
from yaams.watermark import get_watermark, update_watermark


@click.group()
def cli() -> None:
  pass


@cli.command("init-db")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--require-vec", is_flag=True)
def init_db(config_path: str, require_vec: bool) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, require_vec=require_vec)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    seed_entities(conn, _entity_dictionary(cfg))
  finally:
    conn.close()
  click.echo(f"Initialized database: {db_path}")


@cli.command("ingest")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
  "--source",
  default="all",
  show_default=True,
  help="all, imessage, email, teams, or teams_<profile> (e.g. teams_swon)",
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
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    seed_entities(conn, _entity_dictionary(cfg))
    processors = None if dry_run else ProcessingContext(cfg)
    for src in _sources_to_run(source, cfg):
      if not _source_enabled(cfg, src):
        continue
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
    print_stats(conn, db_path, run_stats, dry_run=dry_run)
  finally:
    conn.close()


@cli.command("stats")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
def stats(config_path: str) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    print_stats(conn, db_path, {}, dry_run=False)
  finally:
    conn.close()


@cli.command("reset-db")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
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
@click.option("--config", "config_path", default="config.yaml", show_default=True)
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
) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  query_text = " ".join(text).strip()
  try:
    embedding = None
    if not no_vector:
      embedder = Embedder(**_embed_config(cfg))
      embedding = embedder.embed_batch([query_text])[0]

    qcfg = HybridQueryConfig(
      top_k=top_k,
      source_filter=list(source_filter) or None,
      since=parse_iso_datetime(since) if since else None,
      until=parse_iso_datetime(until) if until else None,
      include_consolidations=not no_consolidations,
    )
    results = run_query(conn, query_text, embedding=embedding, config=qcfg)
  finally:
    conn.close()

  if output_format == "json":
    import json as _json

    click.echo(
      _json.dumps(
        [_result_to_dict(r) for r in results],
        ensure_ascii=False,
        indent=2,
        default=str,
      )
    )
    return

  if not results:
    click.echo("No results.")
    return
  click.echo(f"Top {len(results)} results for: {query_text!r}")
  click.echo()
  for i, r in enumerate(results, 1):
    _render_result(i, r)


def _result_to_dict(r) -> dict:
  return {
    "id": r.id,
    "kind": r.kind,
    "source": r.source,
    "timestamp": r.timestamp.isoformat() if hasattr(r.timestamp, "isoformat") else str(r.timestamp),
    "sender": r.sender,
    "subject": r.subject,
    "thread_id": r.thread_id,
    "score": round(r.score, 4),
    "item_count": r.item_count,
    "participants": r.participants,
    "content_preview": (r.content or "")[:400],
  }


def _render_result(rank: int, r) -> None:
  ts = r.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(r.timestamp, "strftime") else str(r.timestamp)
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
@click.option("--config", "config_path", default="config.yaml", show_default=True)
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
  if source == "email":
    return EmailAdapter(
      sources=list(cfg.get("sources", [])),
      user_addresses=list(cfg.get("user_addresses", [])),
      skip_newsletters=bool(cfg.get("skip_newsletters", True)),
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


def print_stats(
  conn,
  db_path: Path,
  run_stats: dict[str, dict[str, object]],
  *,
  dry_run: bool,
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
  for source in _ordered_sources(run_stats):
    seen = run_stats[source]["seen"]
    new = run_stats[source]["new"]
    skipped = run_stats[source].get("skipped", 0)
    if dry_run:
      suffix = "would process, 0 written"
      if skipped:
        suffix = f"{suffix}, {skipped:,} skipped"
      click.echo(f"  {source}: {seen:,} items ({suffix})")
    else:
      suffix = f"{new:,} new"
      if skipped:
        suffix = f"{suffix}, {skipped:,} skipped"
      click.echo(f"  {source}: {seen:,} items ({suffix})")
    _print_source_diagnostics(source, run_stats[source])
  click.echo(f"  Total in DB: {stats['total']:,} items")
  click.echo(f"  Date range: {_date(stats['date_min'])} to {_date(stats['date_max'])}")
  click.echo(
    f"  Entities in DB: {stats['entities']:,} unique, "
    f"{stats['entity_links']:,} links"
  )
  if db_path.exists():
    click.echo(f"  Storage: {_size_mb(db_path):.1f} MB")


def _effective_since(conn, source: str, cfg: dict) -> datetime:
  configured = parse_iso_datetime(cfg["ingest"]["since"])
  watermark = get_watermark(conn, source)
  floor = datetime.min.replace(tzinfo=UTC)
  return max(configured, watermark or floor)


def _sources_to_run(source: str, cfg: dict | None = None) -> list[str]:
  cfg = cfg or {}
  teams_profiles = list((cfg.get("ingest", {}).get("teams", {}) or {}).get("profiles", []))
  teams_sources = [f"teams_{p}" for p in teams_profiles]
  if source == "all":
    return ["imessage", "email", *teams_sources]
  if source == "teams":
    return teams_sources
  return [source]


def _config_section(source: str) -> str:
  if source.startswith("teams_") or source == "teams":
    return "teams"
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
  if source == "email":
    paths = []
    for entry in source_cfg.get("sources", []):
      source_type = entry.get("type", "unknown")
      path = entry.get("path", "n/a")
      paths.append(f"{source_type}: {Path(path).expanduser()}")
    return paths or ["n/a"]
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
      self._tagger = EntityTagger(
        _entities_config(self.cfg).get("spacy_model"),
        _entity_dictionary(self.cfg),
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


if __name__ == "__main__":
  cli()
