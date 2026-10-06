"""Link message participants (sender + recipients) to person entities.

Entity tagging runs NER over content only, so a message *from* or *to* someone was
never linked to them, and "when did I last speak with X" had nothing to filter on.
Each participant (display name, email, phone) is matched exactly against person
canonical names and aliases (the dictionary is seeded into ``entities`` on every
ingest) and linked with ``item_entities.source = 'participant'``. The owner's own
identities are skipped. Insert-or-ignore: an existing content link is kept as is.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from yaams.time import ensure_utc

_PHONE = re.compile(r"\+?[\d\s\-()]{6,}")


def _norm(value: object) -> str:
  s = (value if isinstance(value, str) else "").strip().lower()
  return re.sub(r"[^\d+]", "", s) if _PHONE.fullmatch(s) else s


def link_participants(
  conn: sqlite3.Connection,
  self_identities: Iterable[str],
  *,
  since: datetime | None = None,
) -> dict[str, int]:
  """Link participants of items ingested at/after ``since`` (all items when None)."""
  lookup: dict[str, int] = {}
  for eid, name, aliases in conn.execute(
    "SELECT id, canonical_name, aliases FROM entities WHERE entity_type = 'person'"
  ):
    try:
      alias_list = json.loads(aliases or "[]")
    except (TypeError, ValueError):
      alias_list = []
    for key in (name, *alias_list):
      if k := _norm(key):
        lookup.setdefault(k, eid)
  skip = {_norm(s) for s in self_identities} | {"me"}
  rows = conn.execute(
    "SELECT id, sender, recipients FROM items WHERE (? IS NULL OR ingested_at >= ?)",
    (None if since is None else ensure_utc(since).isoformat(),) * 2,
  )
  pairs: set[tuple[str, int]] = set()
  for item_id, sender, recipients in rows:
    try:
      parsed = json.loads(recipients) if recipients else []
    except (TypeError, ValueError):
      parsed = []
    people = [sender, *(parsed if isinstance(parsed, list) else [])]
    for p in people:
      k = _norm(p)
      if k and k not in skip and k in lookup:
        pairs.add((item_id, lookup[k]))
  with conn:
    before = conn.total_changes
    conn.executemany(
      "INSERT OR IGNORE INTO item_entities (item_id, entity_id, confidence, source) "
      "VALUES (?, ?, 1.0, 'participant')",
      sorted(pairs),
    )
    added = conn.total_changes - before
  return {"person_keys": len(lookup), "matched": len(pairs), "linked": added}

