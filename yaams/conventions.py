"""YAAMS binding to the shared hugr CLI contract.

The wire contract (action/error envelopes, NDJSON streaming, the
doctor payload shape, the 0-5 exit-code taxonomy, redact()) lives in
the ``hugr-conventions`` package - the executable form of
CONVENTIONS.md in the hugr repo. This module binds it to yaams' tool
name and version and re-exports the names yaams' CLI code and tests
use, so call sites read ``from yaams.conventions import
action_envelope`` without threading ``tool=`` / ``version=`` through
every call.

See https://github.com/damsleth/hugr/blob/main/CONVENTIONS.md.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import hugr_conventions as _hc
from hugr_conventions import (  # re-export: identical wire shapes
  EXIT_AUTH,
  EXIT_NOT_FOUND,
  EXIT_OK,
  EXIT_PARTIAL,
  EXIT_TRANSIENT,
  EXIT_USER_ERROR,
  DoctorFinding,
  emit_action,
  emit_data_error,
  redact,
  stream_progress,
  stream_result,
  stream_warning,
)

__all__ = [
  "EXIT_OK",
  "EXIT_USER_ERROR",
  "EXIT_TRANSIENT",
  "EXIT_AUTH",
  "EXIT_NOT_FOUND",
  "EXIT_PARTIAL",
  "TOOL_NAME",
  "redact",
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


def _version() -> str:
  from yaams import __version__
  return __version__


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
  return _hc.action_envelope(
    tool=TOOL_NAME,
    version=_version,
    command=command,
    ok=ok,
    stats=stats,
    warnings=warnings,
    error=error,
    duration_ms=duration_ms,
  )


def data_error(
  *,
  command: str,
  code: str,
  message: str,
  hint: str | None = None,
) -> dict[str, Any]:
  """Build the minimal error envelope used by data commands on failure."""
  return _hc.data_error(
    tool=TOOL_NAME,
    version=_version,
    command=command,
    code=code,
    message=message,
    hint=hint,
  )


def DoctorPayload(**kwargs: Any) -> _hc.DoctorPayload:  # noqa: N802 - preserves call site
  """yaams-bound :class:`hugr_conventions.DoctorPayload`.

  Defaults ``tool`` to ``"yaams"`` and ``version`` to the live package
  version so existing call sites construct it unchanged.
  """
  kwargs.setdefault("tool", TOOL_NAME)
  kwargs.setdefault("version", _version)
  return _hc.DoctorPayload(**kwargs)
