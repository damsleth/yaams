from __future__ import annotations

import sqlite3
from pathlib import Path

from yaams.config import expand_path


def open_db(
  db_path: str | Path,
  *,
  readonly: bool = False,
  require_vec: bool = False,
) -> sqlite3.Connection:
  path = expand_path(db_path)
  if not readonly:
    path.parent.mkdir(parents=True, exist_ok=True)

  if readonly:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
  else:
    conn = sqlite3.connect(path)

  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  if not readonly:
    conn.execute("PRAGMA journal_mode = WAL")
  _load_sqlite_vec(conn, require_vec=require_vec)
  return conn


def _load_sqlite_vec(conn: sqlite3.Connection, *, require_vec: bool) -> None:
  loaded = False
  try:
    import sqlite_vec

    try:
      conn.enable_load_extension(True)
      sqlite_vec.load(conn)
      loaded = True
    finally:
      try:
        conn.enable_load_extension(False)
      except Exception:
        pass
  except Exception as first_error:
    try:
      conn.enable_load_extension(True)
      conn.load_extension("vec0")
      loaded = True
    except Exception:
      if require_vec:
        raise RuntimeError("sqlite-vec is required but could not be loaded") from first_error
    finally:
      try:
        conn.enable_load_extension(False)
      except Exception:
        pass

  if loaded:
    conn.execute("SELECT vec_version()")
