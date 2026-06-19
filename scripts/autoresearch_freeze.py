#!/usr/bin/env python3
"""Freeze & version the autoresearch scenario (plan step 1).

Snapshots the live DB to a stable fixture and writes a scenario manifest
(`scripts/autoresearch_scenario.json`) recording the gold-label set's identity.
The loop compares fitness only WITHIN one scenario hash — a different gold set
(e.g. after `llm_judge_unjudged.py --apply`) is a different, non-comparable
campaign, so re-freezing is a deliberate act, not a silent drift.

Usage:
    .venv/bin/python scripts/autoresearch_freeze.py            # freeze live -> fixture
    .venv/bin/python scripts/autoresearch_freeze.py --check    # verify current fixture matches manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from yaams.config import get_db_path, load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402

# Canonical frozen fixture lives OUTSIDE the repo so git worktrees (used by the
# parallel driver) all resolve the same absolute path with no env var — and it's
# already the harness's first-choice default DB.
FIXTURE = Path.home() / "brain" / "autoresearch_fixture.db"
MANIFEST = _REPO / "scripts" / "autoresearch_scenario.json"


def _gold_hash(conn) -> tuple[str, int, int]:
    """Stable hash of the gold set: latest hit/correction-with-result per query,
    sorted by (query_id, result_id, kind). Returns (sha1, n_gold, n_corrections)."""
    rows = conn.execute(
        """
        SELECT f.query_id, f.kind, f.result_id
        FROM query_feedback f
        JOIN (SELECT query_id, MAX(id) AS mid FROM query_feedback GROUP BY query_id) last
          ON last.mid = f.id
        WHERE f.kind IN ('hit', 'correction') AND f.result_id IS NOT NULL
        """
    ).fetchall()
    tuples = sorted((r["query_id"], r["result_id"], r["kind"]) for r in rows)
    h = hashlib.sha1(json.dumps(tuples).encode()).hexdigest()
    n_corr = sum(1 for t in tuples if t[2] == "correction")
    return h, len(tuples), n_corr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify fixture matches manifest, don't re-freeze")
    args = ap.parse_args()
    cfg = load_config()

    if args.check:
        if not (FIXTURE.exists() and MANIFEST.exists()):
            print("MISSING fixture or manifest — run freeze first")
            return 1
        manifest = json.loads(MANIFEST.read_text())
        with open_db(str(FIXTURE), readonly=True) as conn:
            h, n, nc = _gold_hash(conn)
        ok = h == manifest["gold_hash"]
        print(f"{'OK' if ok else 'MISMATCH'}: fixture gold_hash={h[:12]} "
              f"manifest={manifest['gold_hash'][:12]} gold={n} corr={nc}")
        return 0 if ok else 2

    FIXTURE.parent.mkdir(exist_ok=True)
    live = str(get_db_path(cfg))
    shutil.copy2(live, FIXTURE)
    with open_db(str(FIXTURE), readonly=True) as conn:
        h, n, nc = _gold_hash(conn)
        n_queries = conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
    MANIFEST.write_text(json.dumps({
        "fixture": str(FIXTURE),
        "source_db": live,
        "gold_hash": h,
        "gold_queries": n,
        "corrections": nc,
        "total_queries": n_queries,
    }, indent=2) + "\n")
    print(f"froze {live}\n  -> {FIXTURE}")
    print(f"  gold={n} corrections={nc} total_queries={n_queries} hash={h[:12]}")
    print(f"  manifest -> {MANIFEST.relative_to(_REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
