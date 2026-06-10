"""Tests for yaams.migrations runtime infrastructure (Phase 1)."""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path
from typing import Callable
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yaams.migrations import Migration, apply_pending, applied, discover


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_migration(name: str, fn: Callable[[sqlite3.Connection], None] | None = None) -> Migration:
    if fn is None:
        def fn(conn: sqlite3.Connection) -> None:
            conn.execute(f"CREATE TABLE IF NOT EXISTS _{name} (id INTEGER PRIMARY KEY)")
    return Migration(name=name, apply=fn)


def _fresh_db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


# ---------------------------------------------------------------------------
# Test: apply_pending creates schema_migrations table on fresh DB
# ---------------------------------------------------------------------------

def test_apply_pending_creates_tracking_table():
    conn = _fresh_db()
    with mock.patch("yaams.migrations.discover", return_value=[]):
        apply_pending(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "schema_migrations" in tables


# ---------------------------------------------------------------------------
# Test: apply_pending is idempotent
# ---------------------------------------------------------------------------

def test_apply_pending_idempotent():
    conn = _fresh_db()
    call_count = {"n": 0}

    def counting_apply(c: sqlite3.Connection) -> None:
        call_count["n"] += 1
        c.execute("CREATE TABLE IF NOT EXISTS _idempotent_test (id INTEGER PRIMARY KEY)")

    migrations = [_make_migration("0001_idempotent", counting_apply)]

    with mock.patch("yaams.migrations.discover", return_value=migrations):
        applied_first = apply_pending(conn)
        applied_second = apply_pending(conn)

    assert applied_first == ["0001_idempotent"]
    assert applied_second == []  # nothing new to apply
    assert call_count["n"] == 1  # apply() called exactly once


# ---------------------------------------------------------------------------
# Test: failure in apply() rolls back that migration
# ---------------------------------------------------------------------------

def test_apply_pending_failure_rolls_back():
    conn = _fresh_db()

    def bad_apply(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE _partial (id INTEGER PRIMARY KEY)")
        raise RuntimeError("intentional failure")

    migrations = [_make_migration("0001_bad", bad_apply)]

    with mock.patch("yaams.migrations.discover", return_value=migrations):
        with pytest.raises(RuntimeError, match="intentional failure"):
            apply_pending(conn)

    # The migration must NOT be recorded in the journal
    applied_set = applied(conn)
    assert "0001_bad" not in applied_set

    # The partial table created inside the transaction should also be gone
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "_partial" not in tables


# ---------------------------------------------------------------------------
# Test: discover() returns migrations in sorted order
# ---------------------------------------------------------------------------

def test_discover_returns_migrations_in_order(tmp_path, monkeypatch):
    """Create synthetic version modules and verify discover() sorts them."""
    # Build a temporary package that looks like yaams.migrations.versions
    pkg_dir = tmp_path / "versions"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")

    names_and_content = [
        ("0003_third.py", 'name = "0003_third"\ndef apply(conn): pass\n'),
        ("0001_first.py", 'name = "0001_first"\ndef apply(conn): pass\n'),
        ("0002_second.py", 'name = "0002_second"\ndef apply(conn): pass\n'),
    ]
    for filename, content in names_and_content:
        (pkg_dir / filename).write_text(content)

    # Build a fake package object and patch it into sys.modules
    fake_pkg = types.ModuleType("yaams.migrations.versions")
    fake_pkg.__path__ = [str(pkg_dir)]  # type: ignore[attr-defined]
    fake_pkg.__name__ = "yaams.migrations.versions"
    fake_pkg.__package__ = "yaams.migrations.versions"

    with monkeypatch.context() as m:
        m.setitem(sys.modules, "yaams.migrations.versions", fake_pkg)
        # Also make importlib.import_module work by adding tmp_path to sys.path
        # and registering modules manually via exec
        import importlib.util

        registered: list[str] = []
        for filename, content in names_and_content:
            mod_name = "yaams.migrations.versions." + filename[:-3]
            spec = importlib.util.spec_from_file_location(mod_name, str(pkg_dir / filename))
            assert spec is not None
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            sys.modules[mod_name] = mod
            registered.append(mod_name)

        try:
            result = discover()
        finally:
            for mod_name in registered:
                sys.modules.pop(mod_name, None)

    assert [m.name for m in result] == ["0001_first", "0002_second", "0003_third"]
