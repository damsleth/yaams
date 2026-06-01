from __future__ import annotations

import sys
import time

import click

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option
from yaams.config import get_db_path, load_config
from yaams.conventions import (
  EXIT_USER_ERROR,
  action_envelope,
  data_error,
  emit_action,
  emit_data_error,
)
from yaams.db import open_db
from yaams.retrieve.associate import build_cooccurrence, resolve_associations
from yaams.schema import init_schema
from yaams.time import utc_now


def _resolve_entity_id(conn, name: str) -> int | None:
  row = conn.execute(
    "SELECT id FROM entities WHERE lower(canonical_name) = ?",
    (name.strip().lower(),),
  ).fetchone()
  return (row[0] if not hasattr(row, "keys") else row["id"]) if row else None


def _name_for(conn, entity_id: int) -> str:
  row = conn.execute(
    "SELECT canonical_name FROM entities WHERE id = ?", (entity_id,)
  ).fetchone()
  return (row[0] if not hasattr(row, "keys") else row["canonical_name"]) if row else f"#{entity_id}"


@cli.group("assoc")
def assoc_group() -> None:
  """Inspect and curate entity associations (learned co-occurrence + manual)."""
  pass


@assoc_group.command("build")
@config_option
@click.option("--min-cooccur", default=3, show_default=True, type=int,
              help="Ignore entity pairs seen together fewer than N times.")
@click.option("--min-score", default=0.15, show_default=True, type=float,
              help="Normalized-PMI floor for a learned edge to be kept.")
@click.option("--json", "as_json", is_flag=True, help="Action envelope on stdout.")
def assoc_build(config_path: str, min_cooccur: int, min_score: float, as_json: bool) -> None:
  """Recompute the learned co-occurrence table from item_entities."""
  start = time.perf_counter()
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    pairs = build_cooccurrence(conn, min_cooccur=min_cooccur, min_score=min_score)
  finally:
    conn.close()
  duration_ms = (time.perf_counter() - start) * 1000
  if as_json:
    emit_action(action_envelope(
      command="assoc build", ok=True,
      stats={"pairs": pairs, "min_cooccur": min_cooccur, "min_score": min_score},
      duration_ms=duration_ms,
    ))
    return
  click.echo(f"Built {pairs} association pair(s) "
             f"(min_cooccur={min_cooccur}, min_score={min_score}).")


@assoc_group.command("show")
@click.argument("entity")
@config_option
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True, help="Raw associations document on stdout.")
def assoc_show(entity: str, config_path: str, limit: int, as_json: bool) -> None:
  """Show entities associated with ENTITY (learned + manual, merged)."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path, readonly=True)
  try:
    eid = _resolve_entity_id(conn, entity)
    if eid is None:
      if as_json:
        emit_data_error(data_error(
          command="assoc show", code="unknown_entity",
          message=f"No entity named {entity!r}",
          hint="List entities with: yaams entities list",
        ))
        sys.exit(EXIT_USER_ERROR)
      click.echo(f"No entity named {entity!r}.", err=True)
      sys.exit(EXIT_USER_ERROR)
    merged = resolve_associations(conn, [eid], min_score=0.0)
    ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    rows = [{"entity": _name_for(conn, t), "weight": round(w, 3)} for t, w in ranked]
  finally:
    conn.close()
  if as_json:
    import json as _json
    click.echo(_json.dumps({"entity": entity, "associations": rows}, ensure_ascii=False))
    return
  if not rows:
    click.echo(f"No associations for {entity!r}. Build them with: yaams assoc build")
    return
  click.echo(f"Associations for {entity!r}:")
  for r in rows:
    click.echo(f"  {r['entity']:<28} {r['weight']:.3f}")


@assoc_group.command("link")
@click.argument("from_entity")
@click.argument("to_entity")
@config_option
@click.option("--kind", default=None, help="Human label for the relation, e.g. located_at.")
@click.option("--weight", default=0.8, show_default=True, type=float,
              help="Override association strength in (0, 1].")
@click.option("--both", is_flag=True, help="Insert the relation in both directions.")
@click.option("--json", "as_json", is_flag=True, help="Action envelope on stdout.")
def assoc_link(from_entity: str, to_entity: str, config_path: str, kind: str | None,
               weight: float, both: bool, as_json: bool) -> None:
  """Manually assert that FROM_ENTITY is associated with TO_ENTITY."""
  _mutate_relation(config_path, from_entity, to_entity, kind=kind, weight=weight,
                   suppress=0, both=both, as_json=as_json, command="assoc link")


@assoc_group.command("suppress")
@click.argument("from_entity")
@click.argument("to_entity")
@config_option
@click.option("--both", is_flag=True, help="Suppress in both directions.")
@click.option("--json", "as_json", is_flag=True, help="Action envelope on stdout.")
def assoc_suppress(from_entity: str, to_entity: str, config_path: str,
                   both: bool, as_json: bool) -> None:
  """Block any (learned or manual) association from FROM_ENTITY to TO_ENTITY."""
  _mutate_relation(config_path, from_entity, to_entity, kind=None, weight=0.0,
                   suppress=1, both=both, as_json=as_json, command="assoc suppress")


@assoc_group.command("unlink")
@click.argument("from_entity")
@click.argument("to_entity")
@config_option
@click.option("--both", is_flag=True, help="Remove the manual relation in both directions.")
@click.option("--json", "as_json", is_flag=True, help="Action envelope on stdout.")
def assoc_unlink(from_entity: str, to_entity: str, config_path: str,
                 both: bool, as_json: bool) -> None:
  """Remove a manual relation, restoring any learned association underneath."""
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    pairs = _resolve_pair(conn, from_entity, to_entity, both, "assoc unlink", as_json)
    with conn:
      for a, b in pairs:
        conn.execute(
          "DELETE FROM entity_relations WHERE from_entity = ? AND to_entity = ?",
          (a, b),
        )
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command="assoc unlink", ok=True,
                                stats={"removed": len(pairs)}))
    return
  click.echo(f"Removed {len(pairs)} manual relation(s).")


def _resolve_pair(conn, from_entity: str, to_entity: str, both: bool,
                  command: str, as_json: bool) -> list[tuple[int, int]]:
  fid = _resolve_entity_id(conn, from_entity)
  tid = _resolve_entity_id(conn, to_entity)
  missing = [n for n, i in ((from_entity, fid), (to_entity, tid)) if i is None]
  if missing:
    msg = f"Unknown entit{'ies' if len(missing) > 1 else 'y'}: {', '.join(missing)}"
    if as_json:
      emit_data_error(data_error(command=command, code="unknown_entity", message=msg,
                                 hint="List entities with: yaams entities list"))
    else:
      click.echo(msg + ".", err=True)
    sys.exit(EXIT_USER_ERROR)
  pairs = [(fid, tid)]
  if both:
    pairs.append((tid, fid))
  return pairs


def _mutate_relation(config_path: str, from_entity: str, to_entity: str, *,
                     kind: str | None, weight: float, suppress: int, both: bool,
                     as_json: bool, command: str) -> None:
  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    pairs = _resolve_pair(conn, from_entity, to_entity, both, command, as_json)
    created = utc_now().isoformat()
    with conn:
      for a, b in pairs:
        conn.execute(
          """
          INSERT INTO entity_relations (from_entity, to_entity, kind, weight, suppress, created_at)
          VALUES (?, ?, ?, ?, ?, ?)
          ON CONFLICT(from_entity, to_entity) DO UPDATE SET
            kind = excluded.kind,
            weight = excluded.weight,
            suppress = excluded.suppress,
            created_at = excluded.created_at
          """,
          (a, b, kind, weight, suppress, created),
        )
  finally:
    conn.close()
  if as_json:
    emit_action(action_envelope(command=command, ok=True,
                                stats={"relations": len(pairs), "suppress": bool(suppress)}))
    return
  verb = "Suppressed" if suppress else "Linked"
  click.echo(f"{verb} {len(pairs)} relation(s).")
