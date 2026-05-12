from __future__ import annotations

import sys
import time

import click

from yaams.config import get_db_path, load_config
from yaams.conventions import EXIT_USER_ERROR, action_envelope, emit_action
from yaams.db import open_db
from yaams.enrich import EntityTagger

from yaams.cli._root import cli
from yaams.cli._shared import _entities_config, _entity_dictionary, config_option


@cli.group("enrich")
def enrich_group() -> None:
  """Re-enrich stored items (tags, embeddings)."""
  pass


@enrich_group.command("retag")
@config_option
@click.option("--source", default=None, help="Limit to a specific source (e.g. imessage).")
@click.option("--batch-size", default=500, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def enrich_retag(config_path: str, source: str | None, batch_size: int, as_json: bool) -> None:
  """Re-tag all stored items with the current NER models and dictionary."""
  t0 = time.monotonic()
  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    conn = open_db(db_path)
  except Exception as exc:
    if as_json:
      emit_action(action_envelope(
        command="enrich retag", ok=False,
        error={"code": "init_failed", "message": str(exc)},
        duration_ms=(time.monotonic() - t0) * 1000.0,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
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
    if not as_json:
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
      if not as_json:
        click.echo(f"  {min(offset, total)}/{total}")
  finally:
    conn.close()
  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="enrich retag", ok=True,
      stats={"total": total, "updated": updated, "source_filter": source, "batch_size": batch_size},
      duration_ms=duration_ms,
    ))
    return
  click.echo(f"Done. Re-tagged {updated} items.")
