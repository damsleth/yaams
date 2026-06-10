from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
import importlib
import pkgutil
import sqlite3


@dataclass
class Migration:
    name: str
    apply: Callable[[sqlite3.Connection], None]
    description: str = ""


def discover() -> list[Migration]:
    """Walk yaams.migrations.versions subpackage, import each NNNN_* module,
    read its module-level 'name' constant and 'apply' function.
    Sort by name for deterministic ordering.
    """
    import yaams.migrations.versions as versions_pkg

    migrations: list[Migration] = []
    prefix = versions_pkg.__name__ + "."
    for finder, module_name, is_pkg in pkgutil.iter_modules(
        versions_pkg.__path__, prefix
    ):
        short = module_name.split(".")[-1]
        # Only load NNNN_* modules
        if not short[0].isdigit():
            continue
        mod = importlib.import_module(module_name)
        name: str = getattr(mod, "name", short)
        apply_fn: Callable[[sqlite3.Connection], None] = mod.apply
        description: str = getattr(mod, "description", "")
        migrations.append(Migration(name=name, apply=apply_fn, description=description))

    migrations.sort(key=lambda m: m.name)
    return migrations


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def applied(conn: sqlite3.Connection) -> set[str]:
    """Return set of migration names from schema_migrations table (if it exists)."""
    try:
        rows = conn.execute("SELECT name FROM schema_migrations").fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        # Table does not exist yet
        return set()


def apply_pending(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> list[str]:
    """Apply all pending migrations in order.

    1. CREATE TABLE IF NOT EXISTS schema_migrations(...)
    2. Read applied names
    3. For each pending migration in order:
       BEGIN IMMEDIATE -> call migration.apply(conn) -> INSERT into schema_migrations -> COMMIT
    4. Return list of names applied (empty if nothing pending)
    """
    # Ensure the tracking table exists (outside any per-migration transaction)
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()

    already_applied = applied(conn)
    all_migrations = discover()
    pending = [m for m in all_migrations if m.name not in already_applied]

    applied_names: list[str] = []
    for migration in pending:
        if dry_run:
            applied_names.append(migration.name)
            continue

        # Use a savepoint so we can rollback only this migration on failure
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration.apply(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (migration.name, now),
            )
            conn.execute("COMMIT")
            applied_names.append(migration.name)
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return applied_names
