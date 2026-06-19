from __future__ import annotations

import sys
import time

import click

from yaams import __version__
from yaams.cli._envelope import JsonFailureGuard
from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, _entity_dictionary, config_option
from yaams.config import get_db_path, load_config
from yaams.conventions import (
  EXIT_USER_ERROR,
  action_envelope,
  emit_action,
)
from yaams.db import open_db
from yaams.enrich.entities import detect_lang
from yaams.schema import init_schema
from yaams.store import backfill_entity_sources, seed_entities


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
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def setup_cmd(config_path: str, as_json: bool) -> None:
  """Install runtime assets (spaCy NER models) into the active Python env."""
  import shutil
  import subprocess

  t0 = time.monotonic()
  try:
    cfg = load_config(config_path)
  except Exception as exc:
    if as_json:
      emit_action(action_envelope(
        command="setup", ok=False,
        error={"code": "config_unreadable", "message": str(exc)},
        duration_ms=(time.monotonic() - t0) * 1000.0,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
  ent_cfg = cfg.get("entities") or {}
  models = [m for m in (ent_cfg.get("spacy_model"), ent_cfg.get("spacy_model_nb")) if m]

  if not models:
    if as_json:
      emit_action(action_envelope(
        command="setup", ok=True,
        stats={"models_installed": [], "models_already_present": [], "models_failed": []},
        duration_ms=(time.monotonic() - t0) * 1000.0,
      ))
    else:
      click.echo("No spaCy models configured under entities.spacy_model[_nb]; nothing to do.")
    return

  import importlib.util
  installed: list[str] = []
  already: list[str] = []
  failed: list[str] = []

  for model in models:
    if importlib.util.find_spec(model) is not None:
      already.append(model)
      if not as_json:
        click.echo(f"  {model}: already installed")
      continue
    if not as_json:
      click.echo(f"  {model}: downloading...")
    # Resolve the wheel URL via spaCy's compat helpers, then install via
    # `python -m pip` directly. spaCy 3.8's `spacy download` looks for `pip`
    # on PATH via shutil.which, which fails in uv-built venvs that don't ship
    # a `pip` entry-point script. We run the resolver in a subprocess because
    # spaCy's helpers sys.exit on lookup failures.
    resolver = (
      "import sys; from urllib.parse import urljoin; "
      "from spacy.cli.download import get_compatibility, get_version, get_model_filename; "
      "from spacy import about; "
      "name = sys.argv[1]; "
      "version = get_version(name, get_compatibility()); "
      "filename = get_model_filename(name, version); "
      "base = about.__download_url__.rstrip('/') + '/'; "
      "print(urljoin(base, filename))"
    )
    resolve = subprocess.run(
      [sys.executable, "-c", resolver, model],
      check=False, capture_output=True, text=True,
    )
    if resolve.returncode != 0:
      failed.append(model)
      if not as_json:
        detail = resolve.stderr.strip() or resolve.stdout.strip() or "wheel URL resolution failed"
        click.echo(f"  {model}: FAILED ({detail})", err=True)
      continue
    url = resolve.stdout.strip()
    # uv-built venvs ship no `pip` module, so prefer `uv pip install` targeting
    # this interpreter; fall back to `python -m pip` where uv isn't installed.
    uv = shutil.which("uv")
    install_cmd = (
      [uv, "pip", "install", "--python", sys.executable, url] if uv
      else [sys.executable, "-m", "pip", "install", url]
    )
    result = subprocess.run(
      install_cmd,
      check=False,
      capture_output=as_json,
    )
    if result.returncode != 0:
      failed.append(model)
      if not as_json:
        click.echo(f"  {model}: FAILED (exit {result.returncode})", err=True)
    else:
      installed.append(model)
      if not as_json:
        click.echo(f"  {model}: ok")

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    ok = not failed
    error = None if ok else {
      "code": "model_install_failed",
      "message": f"Failed to install: {', '.join(failed)}",
      "hint": "Check your network and rerun: yaams setup --json",
    }
    emit_action(action_envelope(
      command="setup",
      ok=ok,
      stats={
        "models_installed": installed,
        "models_already_present": already,
        "models_failed": failed,
      },
      error=error,
      duration_ms=duration_ms,
    ))
    sys.exit(0 if ok else EXIT_USER_ERROR)
  else:
    if failed:
      raise click.ClickException(f"Failed to install: {', '.join(failed)}")
    click.echo("Setup complete.")


@cli.command("init")
@click.option(
  "--path",
  "dest_path",
  default=None,
  help="Destination path. Default: $XDG_CONFIG_HOME/yaams/config.yaml.",
)
@click.option(
  "--force",
  is_flag=True,
  help="Overwrite an existing config file.",
)
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="Emit action envelope on stdout.",
)
def init_cmd(dest_path: str | None, force: bool, as_json: bool) -> None:
  """Write a default YAAMS config to ~/.config/yaams/config.yaml.

  The contents are a copy of ``config.yaml.example`` - edit it before
  running ``yaams ingest`` (the defaults disable everything except
  iMessage and Apple Mail).
  """
  import os
  from importlib import resources

  from yaams.config import expand_path

  t0 = time.monotonic()

  if dest_path is None:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    xdg_root = expand_path(xdg) if xdg else expand_path("~/.config")
    dest = xdg_root / "yaams" / "config.yaml"
  else:
    dest = expand_path(dest_path)

  if dest.is_file() and not force:
    msg = f"Config file already exists: {dest} (use --force to overwrite)"
    if as_json:
      emit_action(action_envelope(
        command="init",
        ok=False,
        error={"code": "exists", "message": msg, "hint": "pass --force or pick a different --path"},
        duration_ms=(time.monotonic() - t0) * 1000.0,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise click.ClickException(msg)

  try:
    body = resources.files("yaams").joinpath("_default_config.yaml").read_text(encoding="utf-8")
  except (FileNotFoundError, ModuleNotFoundError) as exc:
    if as_json:
      emit_action(action_envelope(
        command="init",
        ok=False,
        error={"code": "default_missing", "message": str(exc)},
        duration_ms=(time.monotonic() - t0) * 1000.0,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise click.ClickException(f"Default config missing from the yaams package: {exc}") from exc

  dest.parent.mkdir(parents=True, exist_ok=True)
  dest.write_text(body, encoding="utf-8")

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="init",
      ok=True,
      stats={"path": str(dest), "overwritten": force and dest.is_file()},
      duration_ms=duration_ms,
    ))
  else:
    click.echo(f"Wrote default config to {dest}")
    click.echo("Edit it to enable the sources you want, then run:")
    click.echo("  yaams init-db")
    click.echo("  yaams ingest")


@cli.command("init-db")
@config_option
@click.option("--require-vec", is_flag=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def init_db(config_path: str, require_vec: bool, as_json: bool) -> None:
  t0 = time.monotonic()
  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    created = not db_path.exists()
    conn = open_db(db_path, require_vec=require_vec)
    try:
      init_schema(conn, embedding_dim=_embedding_dim(cfg))
      seed_entities(conn, _entity_dictionary(cfg))
      backfill_entity_sources(conn, _entity_dictionary(cfg))
    finally:
      conn.close()
  except Exception as exc:
    duration_ms = (time.monotonic() - t0) * 1000.0
    if as_json:
      emit_action(action_envelope(
        command="init-db",
        ok=False,
        error={"code": "init_failed", "message": str(exc)},
        duration_ms=duration_ms,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="init-db",
      ok=True,
      stats={"db_path": str(db_path), "created": created},
      duration_ms=duration_ms,
    ))
  else:
    click.echo(f"Initialized database: {db_path}")


@cli.command("stats")
@config_option
@click.option("--json", "as_json", is_flag=True, help="Emit raw stats JSON on stdout.")
def stats(config_path: str, as_json: bool) -> None:
  # Wrap the entire body so config-load and db-open failures surface as
  # data_error envelopes on stdout under --json (Plan 06). The previous
  # implementation only guarded open_db, leaving load_config to traceback.
  with JsonFailureGuard("stats", as_json=as_json):
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    conn = open_db(db_path, readonly=True)
    try:
      from yaams.cli.ingest import print_stats
      from yaams.store import database_stats

      if as_json:
        import json as _json
        payload = dict(database_stats(conn))
        payload["db_path"] = str(db_path)
        if db_path.exists():
          payload["storage_mb"] = round(db_path.stat().st_size / (1024 * 1024), 2)
        click.echo(_json.dumps(payload, ensure_ascii=False))
      else:
        print_stats(conn, db_path, {}, dry_run=False)
    finally:
      conn.close()


@cli.command("reset-db")
@config_option
@click.option("--yes", is_flag=True, help="Skip confirmation. Required when not on a TTY.")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def reset_db(config_path: str, yes: bool, as_json: bool) -> None:
  t0 = time.monotonic()
  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
  except Exception as exc:
    if as_json:
      emit_action(action_envelope(
        command="reset-db", ok=False,
        error={"code": "config_unreadable", "message": str(exc)},
        duration_ms=(time.monotonic() - t0) * 1000.0,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise

  if not yes:
    if as_json or not sys.stdin.isatty():
      # Destructive + non-interactive: refuse rather than prompt.
      if as_json:
        emit_action(action_envelope(
          command="reset-db",
          ok=False,
          error={
            "code": "confirmation_required",
            "message": "reset-db is destructive; pass --yes to confirm",
            "hint": "yaams reset-db --yes --json",
          },
          duration_ms=(time.monotonic() - t0) * 1000.0,
        ))
      else:
        click.echo("reset-db requires --yes when not on a TTY.", err=True)
      sys.exit(EXIT_USER_ERROR)
    click.confirm(f"Delete database at {db_path}?", abort=True)

  removed = False
  if db_path.exists():
    db_path.unlink()
    removed = True

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="reset-db",
      ok=True,
      stats={"db_path": str(db_path), "removed": removed},
      duration_ms=duration_ms,
    ))
  else:
    click.echo(f"Removed database: {db_path}")


@cli.command("backfill-lang")
@config_option
@click.option("--batch-size", default=500, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def backfill_lang(config_path: str, batch_size: int, as_json: bool) -> None:
  """Populate items.lang for rows where it is NULL."""
  t0 = time.monotonic()
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    updated = 0
    cursor = ""
    while True:
      rows = conn.execute(
        "SELECT id, content FROM items WHERE lang IS NULL AND id > ? ORDER BY id LIMIT ?",
        (cursor, batch_size),
      ).fetchall()
      if not rows:
        break
      for row in rows:
        lang = detect_lang(row["content"] or "")
        if lang is not None:
          conn.execute("UPDATE items SET lang = ? WHERE id = ?", (lang, row["id"]))
          updated += 1
      conn.commit()
      cursor = rows[-1]["id"]
      if len(rows) < batch_size:
        break
  finally:
    conn.close()

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="backfill-lang",
      ok=True,
      stats={"updated": updated},
      duration_ms=duration_ms,
    ))
  else:
    click.echo(f"Updated {updated} items with lang tag ({duration_ms:.0f}ms)")
