from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import click

from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.enrich import Embedder, EntityTagger
from yaams.ingest import Adapter, Item
from yaams.ingest.email_mbox import EmailAdapter
from yaams.ingest.imessage import IMessageAdapter
from yaams.schema import DEFAULT_EMBEDDING_DIM, init_schema
from yaams.store import database_stats, seed_entities, store_items
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
  type=click.Choice(["all", "imessage", "email"]),
  default="all",
  show_default=True,
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
    for src in _sources_to_run(source):
      if not _source_enabled(cfg, src):
        continue
      adapter = get_adapter(src, cfg["ingest"][src])
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
    + int(getattr(adapter, "skipped_email_dates", 0)),
    "skipped_emlx": int(getattr(adapter, "skipped_emlx", 0)),
    "skipped_email_dates": int(getattr(adapter, "skipped_email_dates", 0)),
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
    return EmailAdapter(list(cfg.get("sources", [])))
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
  for source in ["imessage", "email"]:
    if source in run_stats:
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


def _sources_to_run(source: str) -> list[str]:
  return ["imessage", "email"] if source == "all" else [source]


def _source_enabled(cfg: dict, source: str) -> bool:
  return bool(cfg.get("ingest", {}).get(source, {}).get("enabled", False))


def _source_paths(source: str, cfg: dict) -> list[str]:
  source_cfg = cfg.get("ingest", {}).get(source, {})
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
  return ["n/a"]


def _print_sources(run_stats: dict[str, dict[str, object]]) -> None:
  if not run_stats:
    return
  click.echo("  Sources:")
  for source in ["imessage", "email"]:
    if source not in run_stats:
      continue
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
    if skipped_emlx or skipped_dates:
      click.echo(
        f"    skipped email details: {skipped_emlx:,} parse errors, "
        f"{skipped_dates:,} invalid dates"
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
