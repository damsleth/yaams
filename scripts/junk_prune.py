#!/usr/bin/env python3
"""Physically remove annotated junk items and reclaim the space.

This is the one step in the data-quality work that is NOT reversible, which is
why it is a script you run, not a maintenance flag: annotation
(`yaams refresh --annotate-junk`) is the default and leaves raw items intact;
this deletes them.

Scope: items with junk_reason LIKE 'mech:%' (or --reason to pick a category)
that are NOT members of a consolidation. Consolidated rows are kept even when
junk: consolidations reference them through raw_item_ids, and deleting a
member orphans the rollup. On 2026-09-16 that excluded 11,902 of 17,443.

Dependents removed with each item: its items_fts row, its items_vec row (needs
the vec0 extension, hence yaams.db.open_db), and its item_entities links.
Then VACUUM, which needs an exclusive lock: run it when nothing else has the
db open (no ingest, no query, no MCP server).

Take a backup first. `sqlite3 data.db ".backup data.db.bak-$(date +%F)"`.

Usage (project venv):
  .venv/bin/python scripts/junk_prune.py             # dry run: counts and MB only
  .venv/bin/python scripts/junk_prune.py --apply     # delete + VACUUM + before/after
  .venv/bin/python scripts/junk_prune.py --apply --reason mech:short
"""
from __future__ import annotations

import argparse
import json
import os
import time

import yaml

from yaams.db import open_db
from yaams.quality import effective_corpus


def main() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("--apply", action="store_true", help="delete and VACUUM (default: dry run)")
  ap.add_argument("--reason", default="mech:%", help="junk_reason LIKE pattern (default mech:%%)")
  ap.add_argument("--report", default=None, help="write before/after JSON here")
  a = ap.parse_args()

  cfg = yaml.safe_load(open(os.path.expanduser("~/.config/yaams/config.yaml")))
  db = os.path.expanduser(cfg.get("db_path") or "~/brain/feed/data.db")
  conn = open_db(db)
  where = "junk_reason LIKE ? AND consolidated_into IS NULL"
  sel = f"SELECT id FROM items WHERE {where}"

  n, mb = conn.execute(
    f"SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) / 1e6 FROM items WHERE {where}", (a.reason,)
  ).fetchone()
  kept = conn.execute(
    "SELECT COUNT(*) FROM items WHERE junk_reason LIKE ? AND consolidated_into IS NOT NULL", (a.reason,)
  ).fetchone()[0]
  print(f"candidates: {n} items, {mb:.2f} MB content  (kept as consolidation members: {kept})")
  before = effective_corpus(conn)
  if not a.apply:
    print(f"dry run. file {before['file_mb']} MB, items {before['items_total']}, "
          f"retrievable {before['items_retrievable']}")
    return

  t = time.time()
  conn.execute("BEGIN")
  d = {
    "item_entities": conn.execute(f"DELETE FROM item_entities WHERE item_id IN ({sel})", (a.reason,)).rowcount,
    "items_vec": conn.execute(f"DELETE FROM items_vec WHERE item_id IN ({sel})", (a.reason,)).rowcount,
    "items_fts": conn.execute(f"DELETE FROM items_fts WHERE item_id IN ({sel})", (a.reason,)).rowcount,
    "items": conn.execute(f"DELETE FROM items WHERE {where}", (a.reason,)).rowcount,
  }
  conn.execute("COMMIT")
  print(f"deleted {d}  ({time.time() - t:.1f}s); vacuuming ...")
  conn.execute("VACUUM")
  after = effective_corpus(conn)
  print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
  for k, label in (("file_mb", "file MB"), ("items_total", "items total"),
                   ("items_retrievable", "retrievable"), ("text_mb_total", "text MB")):
    print(f"  {label:12} {before[k]:>9} -> {after[k]}")
  if a.report:
    json.dump({"before": before, "after": after, "deleted": d}, open(a.report, "w"), indent=1)
    print("report:", a.report)


if __name__ == "__main__":
  main()
