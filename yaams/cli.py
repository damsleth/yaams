from __future__ import annotations

from collections import defaultdict
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
  run_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"seen": 0, "new": 0})
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    seed_entities(conn, _entity_dictionary(cfg))
    embedder = None if dry_run else Embedder(**_embed_config(cfg))
    tagger = None if dry_run else EntityTagger(
      _entities_config(cfg).get("spacy_model"),
      _entity_dictionary(cfg),
    )
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
        embedder=embedder,
        tagger=tagger,
      )
      run_stats[src]["seen"] += source_stats["seen"]
      run_stats[src]["new"] += source_stats["new"]
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
  embedder,
  tagger,
) -> dict[str, int]:
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
      inserted += process_batch(conn, batch, embedder, tagger, dry_run=dry_run)
      batch = []
  if batch:
    inserted += process_batch(conn, batch, embedder, tagger, dry_run=dry_run)
  if not dry_run:
    update_watermark(conn, source, latest_ts)
    conn.commit()
  return {"seen": seen, "new": inserted}


def process_batch(
  conn,
  items: list[Item],
  embedder,
  tagger,
  *,
  dry_run: bool,
) -> int:
  if dry_run:
    return 0
  if embedder is None or tagger is None:
    raise RuntimeError("embedder and tagger are required unless dry_run is set")
  texts = [item.content for item in items]
  embeddings = embedder.embed_batch(texts)
  tags = [tagger.tag(text) for text in texts]
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
  run_stats: dict[str, dict[str, int]],
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
  for source in ["imessage", "email"]:
    if source in run_stats:
      seen = run_stats[source]["seen"]
      new = run_stats[source]["new"]
      click.echo(f"  {source}: {seen:,} items ({new:,} new)")
  click.echo(f"  Total in DB: {stats['total']:,} items")
  click.echo(f"  Date range: {_date(stats['date_min'])} to {_date(stats['date_max'])}")
  click.echo(
    f"  Entities: {stats['entities']:,} unique, "
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


def _embed_config(cfg: dict) -> dict:
  raw = dict(cfg.get("embed", {}))
  model = raw.pop("model")
  return {"model": model, **raw}


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
