"""Link message participants (sender + recipients) to person entities, on a db COPY.

  .venv/bin/python scripts/link_participants.py --db <fixture copy> [--dictionary ~/brain/feed/entities.json]

Entity linking runs NER over content only, so a message *from* or *to* someone is not
linked to them (fixture: Fredrik Nordmoen sent 221 messages, 0 linked). This resolves each
participant (display name, email, phone) against canonical names + aliases (db entities
plus the dictionary's aliases, matched to db entities by canonical name) and inserts
item_entities rows with source='participant'. The owner's own identities are skipped.
Refuses to run on the live db path.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from yaams.cli._shared import _self_identities
from yaams.config import get_db_path, load_config
from yaams.db import open_db


def norm(s):
  s = (s or "").strip().lower()
  if re.fullmatch(r"\+?[\d\s\-()]{6,}", s):
    s = re.sub(r"[^\d+]", "", s)
  return s


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  ap.add_argument("--dictionary", default=str(Path.home() / "brain/feed/entities.json"))
  a = ap.parse_args()
  cfg = load_config()
  if Path(a.db).resolve() == Path(get_db_path(cfg)).resolve():
    sys.exit("refusing to write the live db: run this on a copy")
  conn = open_db(a.db)
  lookup = {}
  ids = {}
  for eid, name, aliases in conn.execute("SELECT id, canonical_name, aliases FROM entities WHERE entity_type='person'"):
    ids[name.lower()] = eid
    lookup[norm(name)] = eid
    for al in json.loads(aliases or "[]"):
      lookup[norm(al)] = eid
  for e in json.load(open(a.dictionary)):
    eid = ids.get(str(e.get("canonical", "")).lower())
    if eid and e.get("type") == "person":
      for al in e.get("aliases") or []:
        lookup.setdefault(norm(al), eid)
  self_ids = {norm(s) for s in _self_identities(cfg)} | {"me"}
  existing = {(i, e) for i, e in conn.execute("SELECT item_id, entity_id FROM item_entities")}
  new, touched = [], 0
  for item_id, sender, recipients in conn.execute("SELECT id, sender, recipients FROM items"):
    people = [sender] + (json.loads(recipients) if recipients and recipients.startswith("[") else [])
    hit = False
    for p in people:
      k = norm(p if isinstance(p, str) else "")
      if not k or k in self_ids:
        continue
      eid = lookup.get(k)
      if eid and (item_id, eid) not in existing:
        existing.add((item_id, eid))
        new.append((item_id, eid, 1.0, "participant"))
        hit = True
    touched += hit
  with conn:
    conn.executemany("INSERT INTO item_entities (item_id, entity_id, confidence, source) VALUES (?, ?, ?, ?)", new)
  print(f"person keys {len(lookup)}; new participant links {len(new)} on {touched} items")


if __name__ == "__main__":
  main()
