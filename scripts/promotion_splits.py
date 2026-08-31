#!/usr/bin/env python3
"""Phase 0 train/dev/holdout splits over the frozen promotion fixture (PR 2).

Assigns every fixture item to exactly one of train/dev/holdout, grouped by
thread so near-identical messages can't leak across splits: all items sharing
a (source, thread_id) land in the same split; threadless items are their own
group. Assignment is a hash of the group key (sha1 mod 10 -> 8/1/1), which is
deliberately NOT a time cut: a group keeps its split forever, so a later
re-freeze that adds items can't move existing holdout evidence into train,
and late-arriving historic data lands in exactly one split. The item universe
itself is still bounded by the fixture's ingestion cursor (see
promotion_scenario.json), which is where the plan's "split on ingested_at,
not source timestamp" requirement is enforced.

The holdout is frozen: no proposer prompt, tuning agent, or gold-set labeling
step may read holdout item content (abbrev_mine.py skips it).

Usage:
    .venv/bin/python scripts/promotion_splits.py            # write scripts/promotion_splits.json
    .venv/bin/python scripts/promotion_splits.py --check    # verify assignment hasn't drifted

The manifest holds only counts and hashes; item content never leaves ~/brain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from yaams.db import open_db  # noqa: E402

FIXTURE = Path.home() / "brain" / "promotion_fixture.db"
MANIFEST = _REPO / "scripts" / "promotion_splits.json"

ALGORITHM = "sha1(group_key) % 10: 0-7 train, 8 dev, 9 holdout; group_key = source:thread_id, else item:id"
SPLITS = ("train", "dev", "holdout")


def group_key(source: str, thread_id: str | None, item_id: str) -> str:
  """Leakage unit: a whole thread moves together; threadless items stand alone."""
  if thread_id:
    return f"{source}:{thread_id}"
  return f"item:{item_id}"


def split_of(source: str, thread_id: str | None, item_id: str) -> str:
  bucket = hashlib.sha1(group_key(source, thread_id, item_id).encode()).digest()[-1] % 10
  if bucket <= 7:
    return "train"
  return "dev" if bucket == 8 else "holdout"


def _assignments(conn) -> list[tuple[str, str]]:
  """Sorted (item_id, split) for every fixture item."""
  rows = conn.execute("SELECT id, source, thread_id FROM items")
  return sorted((str(r["id"]), split_of(r["source"], r["thread_id"], str(r["id"]))) for r in rows)


def _stats(conn) -> dict:
  assignments = _assignments(conn)
  counts = dict.fromkeys(SPLITS, 0)
  for _, split in assignments:
    counts[split] += 1
  return {
    "algorithm": ALGORITHM,
    "item_count": len(assignments),
    "split_counts": counts,
    "assignment_hash": hashlib.sha1(json.dumps(assignments).encode()).hexdigest(),
  }


def write_manifest() -> int:
  if not FIXTURE.exists():
    print("MISSING fixture — run promotion_freeze.py first")
    return 1
  with open_db(FIXTURE, readonly=True) as conn:
    stats = _stats(conn)
  manifest = {
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "fixture": str(FIXTURE),
    **stats,
  }
  MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
  c = stats["split_counts"]
  print(f"splits over {stats['item_count']:,} items: "
        f"train={c['train']:,} dev={c['dev']:,} holdout={c['holdout']:,}")
  print(f"  manifest -> {MANIFEST.relative_to(_REPO)}")
  return 0


def check() -> int:
  if not (FIXTURE.exists() and MANIFEST.exists()):
    print("MISSING fixture or manifest — run promotion_splits.py first")
    return 1
  manifest = json.loads(MANIFEST.read_text())
  with open_db(FIXTURE, readonly=True) as conn:
    stats = _stats(conn)
  problems = [k for k in ("algorithm", "item_count", "assignment_hash")
              if stats[k] != manifest.get(k)]
  if problems:
    print(f"MISMATCH: {', '.join(problems)} — splits drifted; re-derive deliberately")
    return 2
  print(f"OK: splits match manifest ({stats['item_count']:,} items, "
        f"hash={stats['assignment_hash'][:12]})")
  return 0


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--check", action="store_true")
  args = ap.parse_args()
  return check() if args.check else write_manifest()


if __name__ == "__main__":
  raise SystemExit(main())
