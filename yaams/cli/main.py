from __future__ import annotations

import click

from yaams import __version__
from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.schema import init_schema
from yaams.store import backfill_entity_sources, seed_entities

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, _entity_dictionary, config_option


@cli.command("version")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def version_cmd(as_json: bool) -> None:
  """Print yaams version."""
  if as_json:
    import json
    click.echo(json.dumps({"tool": "yaams", "version": __version__}))
  else:
    click.echo(f"yaams {__version__}")


@cli.command("setup")
@config_option
def setup_cmd(config_path: str) -> None:
  """Install runtime assets (spaCy NER models) into the active Python env."""
  import subprocess
  import sys

  cfg = load_config(config_path)
  ent_cfg = cfg.get("entities") or {}
  models = [m for m in (ent_cfg.get("spacy_model"), ent_cfg.get("spacy_model_nb")) if m]
  if not models:
    click.echo("No spaCy models configured under entities.spacy_model[_nb]; nothing to do.")
    return

  import importlib.util
  failed: list[str] = []
  for model in models:
    if importlib.util.find_spec(model) is not None:
      click.echo(f"  {model}: already installed")
      continue
    click.echo(f"  {model}: downloading...")
    result = subprocess.run(
      [sys.executable, "-m", "spacy", "download", model],
      check=False,
    )
    if result.returncode != 0:
      failed.append(model)
      click.echo(f"  {model}: FAILED (exit {result.returncode})", err=True)
    else:
      click.echo(f"  {model}: ok")

  if failed:
    raise click.ClickException(f"Failed to install: {', '.join(failed)}")
  click.echo("Setup complete.")


@cli.command("init-db")
@config_option
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


@cli.command("stats")
@config_option
def stats(config_path: str) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    from yaams.cli.ingest import print_stats

    print_stats(conn, db_path, {}, dry_run=False)
  finally:
    conn.close()


@cli.command("reset-db")
@config_option
@click.option("--yes", is_flag=True)
def reset_db(config_path: str, yes: bool) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  if not yes:
    click.confirm(f"Delete database at {db_path}?", abort=True)
  if db_path.exists():
    db_path.unlink()
  click.echo(f"Removed database: {db_path}")
