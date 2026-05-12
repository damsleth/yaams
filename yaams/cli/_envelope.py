"""JSON failure envelope guard for YAAMS data-class commands.

Background (Plan 06)
--------------------
Action-class commands (init-db, setup, ingest, reset-db) already wrap
their ``load_config`` call in a try/except and emit an action envelope
on failure. Data-class commands (``query``, ``stats``) historically
called ``load_config`` outside any try block, so a missing or malformed
config produced a raw Python traceback on stderr - exit 1 with no JSON
on stdout. Mnem's passthrough wrapper sees that as "tool crashed" and
hides the underlying config error from the user.

This module gives data commands a single, uniform way to satisfy the
mnem CLI contract for ``--json``: stdout is exactly one line of valid
JSON, ok=false on failure, exit code mapped from CONVENTIONS.md.

Usage
-----
    from yaams.cli._envelope import JsonFailureGuard

    @cli.command(...)
    def query(..., as_json: bool):
      with JsonFailureGuard("query", as_json=as_json):
        cfg = load_config(config_path)
        ...  # rest of the command body

The guard is a no-op when ``as_json`` is False - human-mode callers
keep seeing the traceback so debugging stays easy.
"""
from __future__ import annotations

import sqlite3
import sys
import traceback
from contextlib import contextmanager
from typing import Iterator, TextIO

from yaams.conventions import (
  EXIT_NOT_FOUND,
  EXIT_USER_ERROR,
  data_error,
  emit_data_error,
)


def _classify(exc: BaseException) -> tuple[str, str | None, int]:
  """Map an exception to (error_code, hint, exit_code).

  Known classes get a stable code so mnem and other callers can branch
  on it; everything else falls into ``unhandled`` with EXIT_USER_ERROR.

  Exit codes follow CONVENTIONS.md:
    - EXIT_NOT_FOUND (4) for missing config/db files
    - EXIT_USER_ERROR (1) for malformed config and other user-recoverable
      errors
  """
  # YAML parse errors land before FileNotFoundError checks because some
  # yaml.YAMLError subclasses also subclass OSError on certain platforms;
  # we want the parser error to take priority.
  try:
    import yaml  # local import: yaml is a runtime dep but importing
                  # at module top wires it into every import chain.
  except ImportError:  # pragma: no cover - PyYAML is required at runtime
    yaml = None  # type: ignore[assignment]

  if yaml is not None and isinstance(exc, yaml.YAMLError):
    return (
      "config_invalid",
      "Fix the YAML syntax in your config file; `python -m yaml < path` "
      "shows the parse error.",
      EXIT_USER_ERROR,
    )
  if isinstance(exc, FileNotFoundError):
    # Distinguish config-not-found from db-file-not-found by inspecting
    # the missing-file path. ``FileNotFoundError`` carries the filename
    # in ``.filename`` when raised via pathlib/open; fall back to the
    # message for hand-raised cases.
    target = getattr(exc, "filename", None) or str(exc)
    target_lower = target.lower()
    if (
      target_lower.endswith(".yaml")
      or target_lower.endswith(".yml")
      or target_lower.endswith(".yaml'")
      or target_lower.endswith(".yml'")
      or "/yaams/config." in target_lower
      or "/mnem/yaams/config." in target_lower
      or "config" in target_lower
    ):
      return (
        "config_not_found",
        "Run `mnem init` to generate a config, or pass --config <path>.",
        EXIT_NOT_FOUND,
      )
    return (
      "db_open_failed",
      "Run `yaams init-db` to create the database.",
      EXIT_NOT_FOUND,
    )
  if isinstance(exc, sqlite3.OperationalError):
    return (
      "db_open_failed",
      "Run `yaams init-db` or check the db_path in your config.",
      EXIT_USER_ERROR,
    )
  if isinstance(exc, ValueError):
    # ``Config file must contain a mapping`` and ``Config is missing
    # db_path`` both surface as ValueError today.
    return (
      "config_invalid",
      "Check your config.yaml against the example in the repo.",
      EXIT_USER_ERROR,
    )
  return ("unhandled", None, EXIT_USER_ERROR)


@contextmanager
def JsonFailureGuard(
  command: str,
  *,
  as_json: bool,
  stdout: TextIO | None = None,
  stderr: TextIO | None = None,
) -> Iterator[None]:
  """Wrap a data-command body so failures become JSON envelopes.

  Parameters
  ----------
  command:
    The command name as it appears in the envelope (e.g. ``"query"``,
    ``"stats"``). Used verbatim, matching the existing action-envelope
    convention.
  as_json:
    When False the guard is a no-op (the wrapped body runs and any
    exception propagates normally). When True, exceptions inside the
    block are caught, mapped to a ``data_error`` envelope, written as
    one line of JSON to ``stdout``, and the process exits with the
    code from ``_classify``.
  stdout, stderr:
    Optional injection points for tests. Default to ``sys.stdout`` and
    ``sys.stderr``.

  The body's own ``sys.exit`` / ``SystemExit`` is allowed to escape
  unchanged - the guard only catches non-SystemExit exceptions.
  """
  if not as_json:
    yield
    return

  out = stdout if stdout is not None else sys.stdout
  err = stderr if stderr is not None else sys.stderr

  try:
    yield
  except SystemExit:
    raise
  except BaseException as exc:  # noqa: BLE001 - guard is the catch-all
    code, hint, exit_code = _classify(exc)
    envelope = data_error(
      command=command,
      code=code,
      message=str(exc),
      hint=hint,
    )
    emit_data_error(envelope, stream=out)
    # Traceback to stderr only, so stdout stays a single JSON line.
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=err)
    sys.exit(exit_code)
