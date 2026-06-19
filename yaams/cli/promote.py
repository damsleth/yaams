from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from yaams.cli._root import cli
from yaams.cli._shared import _embedding_dim, config_option
from yaams.config import expand_path, get_db_path, load_config
from yaams.conventions import (
  EXIT_USER_ERROR,
  action_envelope,
  data_error,
  emit_action,
  emit_data_error,
)
from yaams.db import open_db
from yaams.schema import init_schema


def _ledger_notes_dir() -> Path | None:
  """Ask cogled where its notes live, via the stable `ledger paths` CLI
  contract (never import cogled as a package). Returns None if cogled is not
  installed or the path is unusable — callers degrade open."""
  import shutil
  import subprocess

  exe = shutil.which("ledger")
  if not exe:
    return None
  try:
    out = subprocess.run(
      [exe, "paths", "--field", "ledger_notes_dir"],
      capture_output=True,
      text=True,
      timeout=10,
    )
  except Exception:
    return None
  if out.returncode != 0:
    return None
  val = out.stdout.strip()
  if not val:
    return None
  notes_dir = Path(val).expanduser()
  return notes_dir if notes_dir.is_dir() else None


def _resolve_inbox_path(promote_cfg_raw: dict) -> Path:
  """Resolve where promoted candidates are written.

  1. explicit `promote.inbox_path` config wins (lets the user override);
  2. else cogled's real inbox: `<ledger_notes_dir>/00_inbox` via `ledger paths`;
  3. else the legacy `~/yaams/ledger-inbox` staging dir (cogled not installed).
  """
  explicit = promote_cfg_raw.get("inbox_path")
  if explicit:
    return expand_path(explicit)
  notes_dir = _ledger_notes_dir()
  if notes_dir is not None:
    return notes_dir / "00_inbox"
  return expand_path("~/yaams/ledger-inbox")


def _resolve_rejected_log_path(
  tier2_cfg_raw: dict,
  promote_cfg_raw: dict,
  note_index_path: Path | None,
) -> Path | None:
  """Resolve cogled's `rejected_candidates.jsonl`, mirroring `note_index_path`.

  1. explicit `ingest.tier2_ledger.rejected_log_path` or
     `promote.rejected_log_path` config wins;
  2. else its sibling next to a known `note_index_path` (same `08_indices` dir);
  3. else derive `<ledger_notes_dir>/08_indices/rejected_candidates.jsonl`.

  May be None (cogled not installed) → the rejection filter degrades open.
  """
  explicit = (
    tier2_cfg_raw.get("rejected_log_path")
    or promote_cfg_raw.get("rejected_log_path")
  )
  if explicit:
    return expand_path(explicit)
  if note_index_path is not None:
    return note_index_path.expanduser().parent / "rejected_candidates.jsonl"
  notes_dir = _ledger_notes_dir()
  if notes_dir is not None:
    return notes_dir / "08_indices" / "rejected_candidates.jsonl"
  return None


@cli.group("promote")
def promote_group() -> None:
  """Generate and review promotion candidates for the Tier 2 ledger."""
  pass


@promote_group.command("generate")
@config_option
@click.option("--days", default=None, type=int, help="Override window_days from config")
@click.option("--min-cluster", default=None, type=int, help="Override min_cluster_items")
@click.option("--entity", default=None, help="Generate for a single entity name only")
@click.option("--json", "as_json", is_flag=True, help="Emit action envelope on stdout.")
def promote_generate(
  config_path: str,
  days: int | None,
  min_cluster: int | None,
  entity: str | None,
  as_json: bool,
) -> None:
  from yaams.promote.candidates import PromoteConfig, generate_candidates, store_candidates
  from yaams.promote.conflict import ConflictConfig
  from yaams.synthesize import llm_adapter_from_config

  t0 = time.monotonic()
  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
  except Exception as exc:
    if as_json:
      emit_action(action_envelope(
        command="promote generate", ok=False,
        error={"code": "config_unreadable", "message": str(exc)},
        duration_ms=(time.monotonic() - t0) * 1000.0,
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
  promote_cfg_raw = cfg.get("promote", {}) or {}
  tier2_cfg_raw = cfg.get("ingest", {}).get("tier2_ledger", {}) or {}
  raw_index_path = (
    tier2_cfg_raw.get("index_path")
    or promote_cfg_raw.get("note_index_path")
  )
  note_index_path = Path(raw_index_path) if raw_index_path else None
  rejected_log_path = _resolve_rejected_log_path(
    tier2_cfg_raw, promote_cfg_raw, note_index_path
  )
  from yaams.promote.dedup import DedupConfig
  sd = promote_cfg_raw.get("semantic_dedup") or {}
  dedup_cfg = DedupConfig(
    enabled=bool(sd.get("enabled", False)),
    duplicate_threshold=float(sd.get("duplicate_threshold", 0.92)),
    merge_threshold=float(sd.get("merge_threshold", 0.80)),
    embed_backend=str(sd.get("embed_backend", "local")),
    ledger_cli=str(sd.get("ledger_cli", "ledger")),
    timeout_s=int(sd.get("timeout_s", 15)),
  )
  if not as_json and dedup_cfg.enabled:
    import shutil
    import subprocess as _sp
    ledger_exe = shutil.which(dedup_cfg.ledger_cli)
    if ledger_exe is None:
      click.echo(
        f"WARNING: semantic_dedup enabled but '{dedup_cfg.ledger_cli}' not found on PATH - dedup unavailable",
        err=True,
      )
    else:
      try:
        _sp.run(
          [ledger_exe, "embed", "--help"],
          capture_output=True, text=True, timeout=5,
        )
      except Exception:
        click.echo(
          "WARNING: 'ledger embed --help' failed - semantic dedup may be unavailable",
          err=True,
        )
  pcfg = PromoteConfig(
    window_days=days or int(promote_cfg_raw.get("window_days", 90)),
    window_days_by_type=dict(promote_cfg_raw.get("window_days_by_type") or {"person": 365}),
    min_cluster_items=min_cluster or int(promote_cfg_raw.get("min_cluster_items", 3)),
    cluster_fetch_k=int(promote_cfg_raw.get("cluster_fetch_k", 10)),
    note_index_path=note_index_path,
    rejected_log_path=rejected_log_path,
    dedup=dedup_cfg,
  )
  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    adapter = llm_adapter_from_config(cfg)
    if not as_json:
      click.echo(f"Generating candidates (window={pcfg.window_days}d, min_cluster={pcfg.min_cluster_items}) ...")
    progress_sink = (lambda *_args, **_kwargs: None) if as_json else click.echo

    # Build ConflictConfig from promote.conflict_detection config block
    conflict_det_raw = promote_cfg_raw.get("conflict_detection") or {}
    conflict_cfg = ConflictConfig(
      enabled=bool(conflict_det_raw.get("enabled", False)),
      only_for_merge_band=bool(conflict_det_raw.get("only_for_merge_band", True)),
      confidence_threshold=float(conflict_det_raw.get("confidence_threshold", 0.7)),
    )

    candidates = generate_candidates(
      conn, adapter, pcfg,
      entity_filter=entity,
      on_progress=progress_sink,
      conflict_cfg=conflict_cfg,
    )
    stored = store_candidates(conn, candidates)
  finally:
    conn.close()

  # Conflict stats
  conflicts_classified = sum(1 for c in candidates if c.conflict_classification is not None)
  duplicates_skipped_llm = 0  # candidates already dropped; can't count post-hoc
  merges_cleared_unrelated = sum(
    1 for c in candidates
    if c.conflict_classification == "unrelated" and c.merge_with is None
  )

  duration_ms = (time.monotonic() - t0) * 1000.0
  if as_json:
    emit_action(action_envelope(
      command="promote generate", ok=True,
      stats={
        "candidates_generated": len(candidates),
        "candidates_stored": stored,
        "window_days": pcfg.window_days,
        "min_cluster_items": pcfg.min_cluster_items,
        "entity_filter": entity,
        "conflicts_classified": conflicts_classified,
        "duplicates_skipped_llm": duplicates_skipped_llm,
        "merges_cleared_unrelated": merges_cleared_unrelated,
      },
      duration_ms=duration_ms,
    ))
    return
  click.echo(f"\nGenerated {len(candidates)} candidates, {stored} new stored.")


@promote_group.command("list")
@config_option
@click.option(
  "--status",
  default="pending",
  type=click.Choice(["pending", "accepted", "rejected", "all"]),
  show_default=True,
)
@click.option("--json", "as_json", is_flag=True, help="Raw candidates document on stdout.")
def promote_list(config_path: str, status: str, as_json: bool) -> None:
  from yaams.promote.candidates import fetch_pending

  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
    conn = open_db(db_path, readonly=True)
  except Exception as exc:
    if as_json:
      emit_data_error(data_error(
        command="promote list", code="db_open_failed", message=str(exc),
        hint="Run: yaams init-db",
      ))
      sys.exit(EXIT_USER_ERROR)
    raise
  try:
    rows = fetch_pending(conn, status)
  finally:
    conn.close()

  if as_json:
    import json as _json
    # Reserved-key contract: no top-level `ok` on data success.
    click.echo(_json.dumps(
      {"status_filter": status, "candidates": [dict(r) for r in rows]},
      ensure_ascii=False,
      default=str,
    ))
    return

  if not rows:
    click.echo(f"No candidates with status={status!r}.")
    return

  click.echo(f"{len(rows)} candidate(s) [{status}]:")
  for r in rows:
    click.echo(
      f"  {r['id'][:8]}  {r['status']:<10}  {r['draft_type']:<12}  "
      f"entity={r['entity']}  title={r['draft_title'][:50]}"
    )


@promote_group.command("review")
@config_option
@click.option("--all", "review_all", is_flag=True, help="Review all statuses, not just pending")
@click.option(
  "--json",
  "as_json",
  is_flag=True,
  help="(Rejected - promote review is interactive; use 'promote list --json' for machine output.)",
)
def promote_review(config_path: str, review_all: bool, as_json: bool) -> None:
  if as_json:
    import sys
    click.echo(
      "promote review is an interactive command; --json is rejected. "
      "Use `yaams promote list --json` for machine-readable candidate data.",
      err=True,
    )
    sys.exit(1)

  from yaams.promote.candidates import (
    fetch_pending,
    update_status,
  )
  from yaams.promote.review import (
    format_note,
    render_candidate,
    write_candidate_to_ledger,
  )

  cfg = load_config(config_path)
  db_path = get_db_path(cfg)
  promote_cfg_raw = cfg.get("promote", {}) or {}
  inbox_path = _resolve_inbox_path(promote_cfg_raw)

  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))
    status_filter = "all" if review_all else "pending"
    candidates = fetch_pending(conn, status_filter)
    if not candidates:
      click.echo("No candidates to review.")
      return

    click.echo(f"Inbox: {inbox_path}")
    total = len(candidates)
    for i, c in enumerate(candidates, 1):
      click.echo(render_candidate(c, i, total))
      while True:
        choice = click.prompt(
          "  [a]ccept  [e]dit  [r]eject  [s]kip  [q]uit",
          default="s",
          prompt_suffix=" > ",
        ).strip().lower()

        if choice == "q":
          click.echo("Review stopped.")
          return

        if choice == "s":
          break

        if choice == "r":
          update_status(conn, c["id"], "rejected")
          click.echo("  Rejected.")
          break

        if choice in ("a", "e"):
          if choice == "e":
            note_content = click.edit(format_note(c)) or format_note(c)
            import json as _j

            from yaams.promote.candidates import mark_items_promoted
            from yaams.promote.candidates import update_status as _us
            from yaams.promote.review import write_to_inbox as _wti
            dest = _wti(c, inbox_path, content=note_content)
            try:
              item_ids = _j.loads(c.get("source_item_ids") or "[]")
            except Exception:
              item_ids = []
            mark_items_promoted(conn, item_ids, str(dest))
            _us(conn, c["id"], "accepted", promoted_path=str(dest))
            click.echo(f"  Accepted -> {dest}")
          else:
            result = write_candidate_to_ledger(conn, c, inbox_path)
            click.echo(f"  Accepted -> {result['ledger_note']}")
          break

        click.echo("  Unknown choice. Use a/e/r/s/q.")

    click.echo("Review complete.")
  finally:
    conn.close()


@promote_group.command("commit")
@config_option
@click.option(
  "--candidate",
  "candidate_ids",
  multiple=True,
  metavar="ID",
  help="Commit one specific candidate (repeatable).",
)
@click.option("--all", "commit_all", is_flag=True, help="Commit all pending candidates.")
@click.option(
  "--min-score",
  type=float,
  default=None,
  metavar="FLOAT",
  help="Commit candidates with signal_score >= FLOAT.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def promote_commit(
  config_path: str,
  candidate_ids: tuple[str, ...],
  commit_all: bool,
  min_score: float | None,
  as_json: bool,
) -> None:
  """Non-interactively commit promotion candidates to the Tier 2 ledger inbox.

  Targeting: supply --all, one or more --candidate <id>, or --min-score <f>.
  All three may be combined. Without any targeting flag the command errors.

  Idempotent: re-committing an already-accepted candidate is a no-op (counted
  as 'skipped', not an error).
  """
  import time as _time

  from yaams.promote.candidates import fetch_pending
  from yaams.promote.review import write_candidate_to_ledger

  t0 = _time.monotonic()
  command = "promote commit"

  if not commit_all and not candidate_ids and min_score is None:
    msg = (
      "No targeting flag given. "
      "Use --all, --candidate <id>, or --min-score <float>."
    )
    if as_json:
      emit_action(action_envelope(
        command=command, ok=False,
        error={"code": "no_target", "message": msg},
      ))
    else:
      click.echo(f"Error: {msg}", err=True)
    sys.exit(EXIT_USER_ERROR)

  try:
    cfg = load_config(config_path)
    db_path = get_db_path(cfg)
  except Exception as exc:
    if as_json:
      emit_action(action_envelope(
        command=command, ok=False,
        error={"code": "config_unreadable", "message": str(exc)},
      ))
    else:
      click.echo(f"Error loading config: {exc}", err=True)
    sys.exit(EXIT_USER_ERROR)

  promote_cfg_raw = cfg.get("promote", {}) or {}
  inbox_path = _resolve_inbox_path(promote_cfg_raw)

  conn = open_db(db_path)
  try:
    init_schema(conn, embedding_dim=_embedding_dim(cfg))

    # Build the candidate pool to commit.
    # With --all we fetch every candidate (pending + accepted) so that
    # already-accepted ones are counted as 'skipped' (idempotency contract).
    # With --min-score alone we only want to commit pending ones that qualify.
    if commit_all:
      all_pending = fetch_pending(conn, "all")
    elif min_score is not None:
      all_pending = fetch_pending(conn, "pending")
    else:
      all_pending = []

    # Merge explicit ids (fetch them regardless of status so we can be idempotent)
    explicit: list[dict] = []
    if candidate_ids:
      rows = fetch_pending(conn, "all")
      by_id = {r["id"]: r for r in rows}
      missing = [cid for cid in candidate_ids if cid not in by_id]
      if missing:
        msg = f"Unknown candidate id(s): {', '.join(missing)}"
        if as_json:
          emit_action(action_envelope(
            command=command, ok=False,
            error={"code": "unknown_candidate", "message": msg},
          ))
        else:
          click.echo(f"Error: {msg}", err=True)
        sys.exit(EXIT_USER_ERROR)
      explicit = [by_id[cid] for cid in candidate_ids]

    # Apply min-score filter on the pending pool
    pool = list(all_pending)
    if min_score is not None:
      pool = [c for c in pool if (c.get("signal_score") or 0.0) >= min_score]

    # Merge explicit + pool, deduplicate by id preserving order
    seen_ids: set[str] = set()
    candidates_to_commit: list[dict] = []
    for c in explicit + pool:
      if c["id"] not in seen_ids:
        seen_ids.add(c["id"])
        candidates_to_commit.append(c)

    items: list[dict] = []
    promoted = 0
    skipped = 0

    for c in candidates_to_commit:
      result = write_candidate_to_ledger(conn, c, inbox_path)
      items.append(result)
      if result["status"] == "written":
        promoted += 1
        if not as_json:
          click.echo(f"  Committed -> {result['ledger_note']}")
      else:
        skipped += 1
        if not as_json:
          click.echo(f"  Skipped (already accepted): {result['candidate_id']}")

    duration_ms = (_time.monotonic() - t0) * 1000.0

    if as_json:
      import json as _json
      envelope = {
        "tool": "yaams",
        "command": command,
        "ok": True,
        "exit_code": 0,
        "promoted": promoted,
        "skipped": skipped,
        "items": items,
        "duration_ms": round(duration_ms, 1),
      }
      click.echo(_json.dumps(envelope, ensure_ascii=False))
    else:
      click.echo(f"\nCommitted {promoted} candidate(s), {skipped} already accepted.")

  finally:
    conn.close()
