"""``yaams --doctor`` - data-class health check.

Per the mnem CONVENTIONS.md spec:

- Output class: data.
- Returns a structured JSON document on stdout (doctor schema) when
  --json is passed; a human-readable report otherwise.
- Exit codes: 0 (ok), 1 (user-fixable findings), 2 (transient), 3
  (auth - not currently a YAAMS concern since auth is in owa-piggy).
"""

from __future__ import annotations

import json

import click

from yaams.conventions import DoctorFinding, DoctorPayload


def run_doctor(config_path: str | None = None) -> DoctorPayload:
  """Run all health checks and return a DoctorPayload.

  Pure function: no side effects on stdout/stderr. Callers print.
  """
  payload = DoctorPayload()

  # --- Config resolution ---------------------------------------------------
  try:
    from yaams.config import resolve_config_path
    cfg_path = resolve_config_path(config_path)
    payload.config_path = str(cfg_path)
    if not cfg_path.is_file():
      payload.findings.append(DoctorFinding(
        id="config_missing",
        severity="error",
        message=f"Config file does not exist: {cfg_path}",
        hint="Run: yaams init (or set $YAAMS_CONFIG)",
      ))
      return payload
  except FileNotFoundError as exc:
    payload.findings.append(DoctorFinding(
      id="config_missing",
      severity="error",
      message=str(exc).splitlines()[0],
      hint="Run: yaams init (or set $YAAMS_CONFIG)",
    ))
    return payload
  except Exception as exc:
    payload.findings.append(DoctorFinding(
      id="config_unreadable",
      severity="error",
      message=f"Could not read config: {exc}",
    ))
    return payload

  # --- Config parses -------------------------------------------------------
  try:
    from yaams.config import load_config
    cfg = load_config(config_path)
  except Exception as exc:
    payload.findings.append(DoctorFinding(
      id="config_invalid",
      severity="error",
      message=f"Could not parse config: {exc}",
      hint=f"Check: {payload.config_path}",
    ))
    return payload

  # --- DB path -------------------------------------------------------------
  try:
    from yaams.config import get_db_path
    db_path = get_db_path(cfg)
    payload.data_path = str(db_path)
    if not db_path.exists():
      payload.findings.append(DoctorFinding(
        id="db_missing",
        severity="warning",
        message="Database file does not exist",
        hint="Run: yaams init-db",
      ))
  except Exception as exc:
    payload.findings.append(DoctorFinding(
      id="db_path_invalid",
      severity="error",
      message=f"db_path is invalid: {exc}",
    ))

  # --- spaCy NER models ---------------------------------------------------
  ent_cfg = cfg.get("entities") or {}
  configured_models = [
    m for m in (ent_cfg.get("spacy_model"), ent_cfg.get("spacy_model_nb")) if m
  ]
  available: list[str] = []
  missing: list[str] = []
  if configured_models:
    import importlib.util
    for m in configured_models:
      if importlib.util.find_spec(m) is not None:
        available.append(m)
      else:
        missing.append(m)
    payload.models = {
      "spacy": {
        "configured": configured_models,
        "available": available,
        "missing": missing,
      }
    }
    for m in missing:
      payload.findings.append(DoctorFinding(
        id=f"spacy_model_missing:{m}",
        severity="error",
        message=f"spaCy model not installed: {m}",
        hint="Run: yaams setup",
      ))

  # --- Embedding model ----------------------------------------------------
  embed_cfg = cfg.get("embed") or {}
  embed_model = embed_cfg.get("model")
  if embed_model:
    payload.models = payload.models or {}
    payload.models["embedding"] = {
      "configured": embed_model,
      # Available-check is intentionally shallow: a full instantiation
      # would download the model (~2GB) which is precisely what doctor
      # MUST NOT do.
    }

  # --- Redaction sentinel smoke test -------------------------------------
  try:
    from yaams.conventions import redact
    sentinel = "CANARY_SECRET_xxxx"
    jwt_like = "eyJalg.payload-" + sentinel + ".sig-padding-123"
    out = redact(f"Bearer {jwt_like}")
    if sentinel in out:
      payload.findings.append(DoctorFinding(
        id="redact_sentinel_leak",
        severity="error",
        message="Redaction sentinel leaked through redact()",
        hint="The redaction utility is not catching expected patterns",
      ))
  except Exception as exc:
    payload.findings.append(DoctorFinding(
      id="redact_unavailable",
      severity="error",
      message=f"redact() is not callable: {exc}",
    ))

  return payload


def _print_human(payload: DoctorPayload) -> None:
  click.echo(f"yaams doctor (v{payload.to_dict()['version']})")
  if payload.config_path:
    click.echo(f"  config: {payload.config_path}")
  if payload.data_path:
    click.echo(f"  db:     {payload.data_path}")
  if payload.models:
    click.echo("  models:")
    for kind, info in payload.models.items():
      click.echo(f"    {kind}: {info}")
  if not payload.findings:
    click.echo("  status: ok")
    return
  click.echo(f"  findings: {len(payload.findings)}")
  for f in payload.findings:
    marker = {"error": "✗", "warning": "!", "info": "·"}.get(f.severity, "·")
    line = f"    {marker} [{f.severity}] {f.id}: {f.message}"
    click.echo(line)
    if f.hint:
      click.echo(f"        hint: {f.hint}")


def emit_doctor(config_path: str | None, as_json: bool) -> int:
  """Run doctor and emit output. Returns the exit code."""
  payload = run_doctor(config_path)
  if as_json:
    click.echo(json.dumps(payload.to_dict(), ensure_ascii=False))
  else:
    _print_human(payload)
  return payload.exit_code()
