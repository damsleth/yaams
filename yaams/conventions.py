"""YAAMS implementation of the hugr suite CLI contract.

The wire contract (action/error envelopes, NDJSON streaming, the
doctor payload shape, the 0-5 exit-code taxonomy, redact()) is
specified by CONVENTIONS.md in the hugr repo. YAAMS keeps a
self-contained hand-copy of it here rather than depending on a
separate package, so yaams installs cleanly with no third-party
runtime dependency and stays independently shippable - the suite's
loose-coupling axiom. Mirrors the equivalent files in
cognitive-ledger, owa-piggy, and owa-tools.

See https://github.com/damsleth/hugr/blob/main/CONVENTIONS.md.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, TextIO

__all__ = [
  "EXIT_OK",
  "EXIT_USER_ERROR",
  "EXIT_TRANSIENT",
  "EXIT_AUTH",
  "EXIT_NOT_FOUND",
  "EXIT_PARTIAL",
  "TOOL_NAME",
  "redact",
  "now_iso",
  "action_envelope",
  "emit_action",
  "data_error",
  "emit_data_error",
  "stream_progress",
  "stream_warning",
  "stream_result",
  "DoctorFinding",
  "DoctorPayload",
]

TOOL_NAME = "yaams"


# --- Exit codes (CONVENTIONS.md "Exit codes") ------------------------------

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_TRANSIENT = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_PARTIAL = 5


# --- Redaction (CONVENTIONS.md "Redaction") --------------------------------

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+")
_TOKEN_FIELD_RE = re.compile(
  r'(?i)"(access_token|refresh_token|id_token|client_secret|api_key|secret)"\s*:\s*"[^"]*"'
)
_BODY_FIELD_RE = re.compile(
  r'(?i)"(body|content|text|html_body|plain_body)"\s*:\s*"[^"]*"'
)


def redact(text: Any) -> str:
  """Redact secret-shaped substrings from text bound for stderr/logs."""
  if text is None:
    return ""
  if not isinstance(text, str):
    text = str(text)
  text = _JWT_RE.sub("<redacted-jwt>", text)
  text = _BEARER_RE.sub("Bearer <redacted>", text)
  text = _TOKEN_FIELD_RE.sub(lambda m: f'"{m.group(1)}":"<redacted>"', text)
  text = _BODY_FIELD_RE.sub(lambda m: f'"{m.group(1)}":"<redacted>"', text)
  return text


def now_iso() -> str:
  return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- internals -------------------------------------------------------------

def _version() -> str:
  from yaams import __version__
  return __version__


def _writeln(obj: Mapping[str, Any], stream: TextIO | None) -> None:
  out = stream if stream is not None else sys.stdout
  out.write(json.dumps(obj, ensure_ascii=False) + "\n")
  out.flush()


# --- Action envelope -------------------------------------------------------

def action_envelope(
  *,
  command: str,
  ok: bool,
  stats: Mapping[str, Any] | None = None,
  warnings: Iterable[str] | None = None,
  error: Mapping[str, Any] | None = None,
  duration_ms: float | None = None,
) -> dict[str, Any]:
  """Build the standard action envelope dict.

  Invariant: caller MUST exit nonzero when ``ok=False`` and zero when
  ``ok=True``. The envelope and exit code are required to agree.
  """
  return {
    "tool": TOOL_NAME,
    "version": _version(),
    "command": command,
    "ok": bool(ok),
    "duration_ms": float(duration_ms) if duration_ms is not None else 0.0,
    "stats": dict(stats or {}),
    "warnings": list(warnings or []),
    "error": dict(error) if error else None,
  }


def emit_action(envelope: Mapping[str, Any], stream: TextIO | None = None) -> None:
  _writeln(envelope, stream)


# --- Data-class failure envelope -------------------------------------------

def data_error(
  *,
  command: str,
  code: str,
  message: str,
  hint: str | None = None,
) -> dict[str, Any]:
  """Build the minimal error envelope used by data commands on failure."""
  err: dict[str, Any] = {"code": code, "message": message}
  if hint:
    err["hint"] = hint
  return {
    "tool": TOOL_NAME,
    "version": _version(),
    "command": command,
    "ok": False,
    "error": err,
  }


def emit_data_error(envelope: Mapping[str, Any], stream: TextIO | None = None) -> None:
  _writeln(envelope, stream)


# --- NDJSON streaming ------------------------------------------------------

def stream_progress(
  *,
  source: str | None = None,
  stage: str | None = None,
  done: int | None = None,
  total: int | None = None,
  stream: TextIO | None = None,
) -> None:
  payload: dict[str, Any] = {"type": "progress", "ts": now_iso()}
  if source is not None:
    payload["source"] = source
  if stage is not None:
    payload["stage"] = stage
  if done is not None:
    payload["done"] = done
  if total is not None:
    payload["total"] = total
  _writeln(payload, stream)


def stream_warning(
  message: str, *, source: str | None = None, stream: TextIO | None = None
) -> None:
  payload: dict[str, Any] = {"type": "warning", "message": redact(message), "ts": now_iso()}
  if source is not None:
    payload["source"] = source
  _writeln(payload, stream)


def stream_result(envelope: Mapping[str, Any], stream: TextIO | None = None) -> None:
  _writeln({"type": "result", **dict(envelope)}, stream)


# --- Doctor payload --------------------------------------------------------

@dataclass
class DoctorFinding:
  id: str
  severity: str  # "info" | "warning" | "error"
  message: str
  hint: str | None = None

  def to_dict(self) -> dict[str, Any]:
    out: dict[str, Any] = {
      "id": self.id,
      "severity": self.severity,
      "message": self.message,
    }
    if self.hint:
      out["hint"] = self.hint
    return out


@dataclass
class DoctorPayload:
  tool: str = TOOL_NAME
  version: "str | Callable[[], str]" = field(default_factory=lambda: _version)
  config_path: str | None = None
  data_path: str | None = None
  auth: dict[str, Any] | None = None
  models: dict[str, Any] | None = None
  findings: list[DoctorFinding] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    v = self.version() if callable(self.version) else self.version
    out: dict[str, Any] = {"tool": self.tool, "version": str(v)}
    if self.config_path is not None:
      out["config_path"] = self.config_path
    if self.data_path is not None:
      out["data_path"] = self.data_path
    if self.auth is not None:
      out["auth"] = self.auth
    if self.models is not None:
      out["models"] = self.models
    out["findings"] = [f.to_dict() for f in self.findings]
    return out

  def exit_code(self) -> int:
    severities = {f.severity for f in self.findings}
    if "error" in severities:
      return EXIT_USER_ERROR
    return EXIT_OK
