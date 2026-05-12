"""File + stderr logging configuration for YAAMS CLI commands.

Writes a rotating-by-date log file into <db_dir>/logs/yaams-YYYY-MM-DD.log so
ingest, retrieval, and other long-running commands leave a trail. The DB
directory is used as the root because that's the only filesystem location
YAAMS is guaranteed to own (db_path is required in config).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False


def log_dir_for_db(db_path: Path) -> Path:
  return db_path.expanduser().resolve().parent / "logs"


def setup_logging(
  db_path: Path | None = None,
  *,
  verbose: bool = False,
  log_dir: Path | None = None,
) -> Path | None:
  """Configure root yaams logger. Idempotent.

  Returns the log file path or None if no db_path/log_dir was resolvable.
  """
  global _CONFIGURED

  level = logging.DEBUG if verbose else logging.INFO
  root = logging.getLogger("yaams")
  root.setLevel(level)

  if _CONFIGURED:
    for handler in root.handlers:
      handler.setLevel(level)
    return getattr(root, "_yaams_log_file", None)

  fmt = logging.Formatter(
    "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
  )

  stderr_level = logging.DEBUG if verbose else logging.WARNING
  stderr = logging.StreamHandler(sys.stderr)
  stderr.setLevel(stderr_level)
  stderr.setFormatter(fmt)
  root.addHandler(stderr)

  log_file: Path | None = None
  resolved_dir = log_dir or (log_dir_for_db(db_path) if db_path else None)
  if resolved_dir is not None:
    try:
      resolved_dir.mkdir(parents=True, exist_ok=True)
      today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
      log_file = resolved_dir / f"yaams-{today}.log"
      fh = logging.FileHandler(log_file, encoding="utf-8")
      fh.setLevel(logging.DEBUG)
      fh.setFormatter(fmt)
      root.addHandler(fh)
    except OSError as exc:
      root.warning("Could not open log file under %s: %s", resolved_dir, exc)
      log_file = None

  root.propagate = False
  root._yaams_log_file = log_file  # type: ignore[attr-defined]
  _CONFIGURED = True

  pid = os.getpid()
  root.info("logging initialized pid=%s level=%s file=%s", pid, logging.getLevelName(level), log_file)
  return log_file
