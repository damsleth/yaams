"""Backfill participant links (yaams.enrich.participants) over every item.

  .venv/bin/python scripts/link_participants.py --db <fixture copy>
  .venv/bin/python scripts/link_participants.py --live      # the configured db

Ingest links new items on every run; this is the one-off for history, and for
relinking after new people or aliases land in the dictionary. Idempotent.
"""
import argparse
import sys
from pathlib import Path

from yaams.cli._shared import _self_identities
from yaams.config import get_db_path, load_config
from yaams.db import open_db
from yaams.enrich.participants import link_participants


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db")
  ap.add_argument("--live", action="store_true", help="write the configured live db")
  a = ap.parse_args()
  cfg = load_config()
  live = Path(get_db_path(cfg)).resolve()
  db = live if a.live else Path(a.db or sys.exit("--db or --live required")).resolve()
  if db == live and not a.live:
    sys.exit("that is the live db: pass --live to write it")
  conn = open_db(str(db))
  print(link_participants(conn, _self_identities(cfg)))


if __name__ == "__main__":
  main()
