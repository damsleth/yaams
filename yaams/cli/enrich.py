from __future__ import annotations

import click

from yaams.config import get_db_path, load_config
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
