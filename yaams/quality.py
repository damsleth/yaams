"""Retrieval-quality annotation of raw items.

Raw items are immutable in content. What this module does is *annotate*: it
sets ``items.junk_reason`` on rows that carry no retrievable content, so the
retrieval layer can skip them when ``retrieve.exclude_junk`` is on. Nothing is
deleted, every reason is prefixed so a category can be reversed with one
UPDATE, and an item already annotated is never re-labelled.

Only the mechanical rules live here. They were chosen against the live corpus
on 2026-09-16, where 32% of iMessages are under 10 characters, and guarded
against the two false positives an unguarded pass would make:

* exact-duplicate *content* is not junk -- "ok" sent 400 times across threads
  is 400 events, and first/last-occurrence queries depend on them. A duplicate
  is the same content in the same thread from the same sender on the same day
  (9,199 -> 1,875 on the live corpus).
* calendar repeats are recurrences, not duplicates (332 -> 1 when keyed by
  timestamp). Calendar sources are excluded from every rule.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from yaams.store import chunked

MECH_SHORT = "mech:short"
MECH_REACTION = "mech:reaction"
MECH_DUP = "mech:dup"

# Rules apply to conversational sources only. Long-form sources (notes, chats,
# github, agent_memory, tier2) measured clean and are the knowledge; calendars
# are excluded because short titles and recurrences are both legitimate.
_MESSAGING_SOURCES_SQL = "(source = 'imessage' OR source = 'signal' OR source LIKE 'teams%')"

# iMessage tapbacks arrive as text. Reaction-shaped rows are excluded from
# retrieval but kept under their own reason: they may become an affirmation
# signal later.
_REACTION_SQL = (
  "(content LIKE 'Liked %' OR content LIKE 'Loved %' OR content LIKE 'Emphasized %' "
  "OR content LIKE 'Laughed at %' OR content LIKE 'Questioned %' OR content LIKE 'Disliked %')"
)

SHORT_MAX_CHARS = 10


def _ids(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[str]:
  return [row[0] for row in conn.execute(sql, params)]


def _set_reason(conn: sqlite3.Connection, ids: list[str], reason: str) -> int:
  n = 0
  for chunk in chunked(ids):
    placeholders = ",".join("?" * len(chunk))
    cur = conn.execute(
      f"UPDATE items SET junk_reason = ? WHERE junk_reason IS NULL AND id IN ({placeholders})",
      (reason, *chunk),
    )
    n += cur.rowcount
  return n


def find_short(conn: sqlite3.Connection) -> list[str]:
  return _ids(
    conn,
    f"SELECT id FROM items WHERE junk_reason IS NULL AND {_MESSAGING_SOURCES_SQL} "
    f"AND length(trim(content)) < ? AND NOT {_REACTION_SQL}",
    (SHORT_MAX_CHARS,),
  )


def find_reactions(conn: sqlite3.Connection) -> list[str]:
  return _ids(
    conn,
    f"SELECT id FROM items WHERE junk_reason IS NULL AND source = 'imessage' AND {_REACTION_SQL}",
  )


def find_duplicates(conn: sqlite3.Connection) -> list[str]:
  """Every row but the earliest of a (content, thread, sender, day) group."""
  return _ids(
    conn,
    f"""
    SELECT i.id FROM items i
    JOIN (
      SELECT content, thread_id, sender, date(timestamp) AS day, MIN(id) AS keep
      FROM items
      WHERE {_MESSAGING_SOURCES_SQL} AND junk_reason IS NULL
      GROUP BY content, thread_id, sender, day
      HAVING COUNT(*) > 1
    ) g ON g.content = i.content AND g.thread_id IS i.thread_id
       AND g.sender IS i.sender AND g.day = date(i.timestamp)
    WHERE i.id != g.keep AND i.junk_reason IS NULL AND {_MESSAGING_SOURCES_SQL.replace('source', 'i.source')}
    """,
  )


def annotate_mechanical(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, Any]:
  """Apply the three mechanical rules. Returns per-reason counts.

  Order matters only for attribution: a row that is both short and a
  duplicate is labelled short, since that is the cheaper thing to explain.
  """
  short = find_short(conn)
  reactions = find_reactions(conn)
  stats: dict[str, Any] = {"dry_run": dry_run}
  if dry_run:
    dups = find_duplicates(conn)
    stats.update({MECH_SHORT: len(short), MECH_REACTION: len(reactions), MECH_DUP: len(dups)})
    return stats
  stats[MECH_SHORT] = _set_reason(conn, short, MECH_SHORT)
  stats[MECH_REACTION] = _set_reason(conn, reactions, MECH_REACTION)
  # duplicates are found after the other two are written, so their groups
  # only count rows that are still retrievable
  stats[MECH_DUP] = _set_reason(conn, find_duplicates(conn), MECH_DUP)
  conn.commit()
  return stats


def effective_corpus(conn: sqlite3.Connection) -> dict[str, Any]:
  """Physical vs retrievable size, for before/after reporting."""
  phys_bytes = conn.execute(
    "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()"
  ).fetchone()[0]
  total, total_mb = conn.execute("SELECT COUNT(*), SUM(LENGTH(content)) / 1e6 FROM items").fetchone()
  live, live_mb = conn.execute(
    "SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) / 1e6 FROM items WHERE junk_reason IS NULL"
  ).fetchone()
  by_reason = dict(
    conn.execute(
      "SELECT junk_reason, COUNT(*) FROM items WHERE junk_reason IS NOT NULL GROUP BY junk_reason"
    ).fetchall()
  )
  return {
    "file_mb": round(phys_bytes / 1e6, 1),
    "items_total": total,
    "text_mb_total": round(total_mb or 0, 1),
    "items_retrievable": live,
    "text_mb_retrievable": round(live_mb or 0, 1),
    "annotated": by_reason,
  }
