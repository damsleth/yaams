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
from datetime import datetime, timedelta

from yaams.time import ensure_utc

_PHONE = re.compile(r"\+?[\d\s\-()]{6,}")


def norm_identity(value: object) -> str:
  s = (value if isinstance(value, str) else "").strip().lower()
  return re.sub(r"[^\d+]", "", s) if _PHONE.fullmatch(s) else s


def conversation_items(
  conn: sqlite3.Connection,
  item_ids: Iterable[str],
  person: Iterable[str],
  owner: Iterable[str],
  window_hours: float = 2,
) -> set[str]:
  """The items among `item_ids` that are a conversation between the user and a person.

  An item counts when the person or the user sent it and either it went to exactly
  one recipient (a 1:1 chat or mail) or the other party also wrote in the same
  thread within `window_hours`. A third party's message to a group both are in, or
  the person's broadcast to a group the user never answered, is not "speaking with"
  them (the owner's gold labels for "when did I last speak with X" are 1:1 chats).
  `person` and `owner` are identities (names, aliases, emails, phones)."""
  p_keys = {norm_identity(x) for x in person}
  o_keys = {norm_identity(x) for x in owner} | {"me"}
  by_thread: dict[str, list[tuple[str, datetime, str, bool]]] = {}
  ids = list(item_ids)
  for start in range(0, len(ids), 900):
    chunk = ids[start:start + 900]
    for iid, thread, ts, sender, recipients in conn.execute(
      f"SELECT id, thread_id, timestamp, sender, recipients FROM items WHERE id IN ({','.join('?' * len(chunk))})",
      chunk,
    ):
      key = norm_identity(sender)
      role = "o" if key in o_keys else "p" if key in p_keys else None
      if role is None:
        continue
      try:
        parsed = json.loads(recipients) if recipients else []
      except (TypeError, ValueError):
        parsed = []
      to = parsed if isinstance(parsed, list) else []
      # direct: exactly one recipient, and it is the other party
      direct = len(to) == 1 and norm_identity(to[0]) in (p_keys if role == "o" else o_keys)
      by_thread.setdefault(thread or iid, []).append((iid, ensure_utc(datetime.fromisoformat(ts)), role, direct))
  window = timedelta(hours=window_hours)
  keep: set[str] = set()
  for msgs in by_thread.values():
    for iid, ts, role, is_direct in msgs:
      if is_direct or any(r != role and abs(t - ts) <= window for _, t, r, _ in msgs):
        keep.add(iid)
  return keep


def link_participants(
  conn: sqlite3.Connection,
  self_identities: Iterable[str],
  *,
  since: datetime | None = None,
) -> dict[str, int]:
  """Link participants of items ingested at/after ``since`` (all items when None)."""
  # A canonical name wins over another person's alias; an alias two people share
  # ("Alex") links neither, since a wrong confidence-1.0 link becomes a hard filter.
  canon: dict[str, int] = {}
  alias: dict[str, set[int]] = {}
  for eid, name, aliases in conn.execute(
    "SELECT id, canonical_name, aliases FROM entities WHERE entity_type = 'person' AND pending_review != 2"
  ):
    if k := norm_identity(name):
      canon.setdefault(k, eid)
    try:
      alias_list = json.loads(aliases or "[]")
    except (TypeError, ValueError):
      alias_list = []
    for a in alias_list if isinstance(alias_list, list) else []:
      if k := norm_identity(a):
        alias.setdefault(k, set()).add(eid)
  lookup = {k: next(iter(v)) for k, v in alias.items() if len(v) == 1}
  lookup.update(canon)
  skip = {norm_identity(s) for s in self_identities} | {"me"}
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
      k = norm_identity(p)
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

