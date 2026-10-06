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
  """Annotate `ids`, except an item that was ever a hit/correction answer: no rule,
  mechanical or model, may hide a gold answer."""
  n = 0
  for chunk in chunked(ids):
    placeholders = ",".join("?" * len(chunk))
    cur = conn.execute(
      f"UPDATE items SET junk_reason = ? WHERE junk_reason IS NULL AND id IN ({placeholders}) "
      "AND id NOT IN (SELECT result_id FROM query_feedback "
      "WHERE kind IN ('hit', 'correction') AND result_id IS NOT NULL)",
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


# --- model pass: a fine-tuned junk classifier (Jeff) over the band the mechanical
# rules cannot decide. Opt-in (`quality.junk_model.enabled`); see docs/user-guide.md.
# The rendering below is the training contract of the fine-tune
# (scripts/jeff_junk_ftdata.py imports it): change it and the model must be retrained.

MODEL_BAND = (10, 39)  # trimmed characters; below is mech:short, above was never labelled
MODEL_REASON = "llm:junk-jeff"
MODEL_CRITERION = "The TARGET message is junk: it carries no retrievable content on its own."
MODEL_STATE = {"task": "Junk filter for a personal search index over Kim's chat messages."}
CONTEXT_CHARS = 120


def message_context(conn: sqlite3.Connection, row: dict) -> tuple[str, str]:
  """The previous and next message in the row's thread, 120 chars each."""
  prev = conn.execute(
    "SELECT content FROM items WHERE thread_id=? AND timestamp<? ORDER BY timestamp DESC LIMIT 1",
    (row["thread_id"], row["timestamp"]),
  ).fetchone()
  nxt = conn.execute(
    "SELECT content FROM items WHERE thread_id=? AND timestamp>? ORDER BY timestamp ASC LIMIT 1",
    (row["thread_id"], row["timestamp"]),
  ).fetchone()
  return (prev[0] if prev else "")[:CONTEXT_CHARS], (nxt[0] if nxt else "")[:CONTEXT_CHARS]


def junk_block(conn: sqlite3.Connection, row: dict) -> str:
  p, n = message_context(conn, row)
  return f"[{row['source']}] prev: {p!r}\nTARGET: {row['content'].strip()!r}\nnext: {n!r}"


def _serve(url: str, cmd: str | None, wait_s: int = 180):
  """A Popen for a server started here, None if one already answers. Raises if none comes up."""
  import shlex
  import subprocess
  import time
  import urllib.request

  health = url.rsplit("/v1/", 1)[0] + "/health"

  def ready() -> bool:
    try:
      with urllib.request.urlopen(health, timeout=2) as r:
        return b'"ready"' in r.read()
    except OSError:
      return False

  if ready():
    return None
  if not cmd:
    raise RuntimeError(f"no junk model server at {health} and no serve_cmd configured")
  proc = subprocess.Popen(shlex.split(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  for _ in range(wait_s):
    if ready():
      return proc
    if proc.poll() is not None:
      raise RuntimeError(f"serve_cmd exited with {proc.returncode}")
    time.sleep(1)
  proc.terminate()
  raise RuntimeError(f"junk model server not ready after {wait_s}s")


def annotate_model(conn: sqlite3.Connection, cfg: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
  """Label the 10-39 char messaging band with the fine-tuned junk model.

  Only rows still unannotated and ingested at/after `since` (an earlier bulk
  relabel covers what came before). An item that was ever a hit/correction
  answer is never hidden. Scores are cached (yaams.jev), so a KEEP row re-seen
  next run costs a lookup, not a model call. A missing server is a note, never
  an ingest failure.
  """
  from yaams import jev

  lo, hi = MODEL_BAND
  params: list[Any] = [lo, hi]
  since_sql = ""
  if cfg.get("since"):
    since_sql = " AND ingested_at >= ?"
    params.append(str(cfg["since"]))
  rows = [dict(r) for r in conn.execute(
    f"SELECT id, source, thread_id, timestamp, content FROM items WHERE junk_reason IS NULL "
    f"AND {_MESSAGING_SOURCES_SQL} AND length(trim(content)) BETWEEN ? AND ?{since_sql}", params)]
  protected = {r[0] for r in conn.execute(
    "SELECT DISTINCT result_id FROM query_feedback "
    "WHERE kind IN ('hit', 'correction') AND result_id IS NOT NULL")}
  rows = [r for r in rows if r["id"] not in protected]
  stats: dict[str, Any] = {"candidates": len(rows)}
  if not rows:
    return stats
  import time

  url = cfg.get("url", "http://127.0.0.1:8766/v1/systemone")
  t0 = time.perf_counter()
  texts = {r["id"]: junk_block(conn, r) for r in rows}
  t_render = time.perf_counter()
  proc = None
  try:
    proc = _serve(url, cfg.get("serve_cmd"))
    t_served = time.perf_counter()
    stats["server_started"] = proc is not None
    stats["server_start_s"] = round(t_served - t_render, 2)
    scores = jev.noul(cfg.get("state", MODEL_STATE), texts, MODEL_CRITERION,
                      criterion_version=cfg.get("criterion_version", "junk-owner-v2"),
                      tag="ingest_junk", workers=1, url=url, model=cfg.get("model", "jeff-latest"),
                      cache_model=cfg.get("cache_model", "jeff-junk-v2"),
                      timeout=float(cfg.get("timeout_s", 120)),
                      deadline_s=float(cfg.get("deadline_s", 900)))
  except Exception as exc:  # noqa: BLE001 -- the junk pass must never fail an ingest
    stats["note"] = f"skipped: {exc}"
    return stats
  finally:
    if proc is not None:  # stop what we started, and wait: a cron run must not leave it resident
      proc.terminate()
      try:
        proc.wait(timeout=30)
      except Exception:  # noqa: BLE001
        proc.kill()
  t_scored = time.perf_counter()
  tau = float(cfg.get("threshold", 0.73))
  junk = [i for i, s in scores.items() if s >= tau]
  score_s = t_scored - t_served
  stats.update({"scored": len(scores), "threshold": tau, "render_s": round(t_render - t0, 2),
                "score_s": round(score_s, 2),
                "rows_per_s": round(len(scores) / score_s, 1) if score_s > 0 else None,
                "total_s": round(time.perf_counter() - t0, 2)})
  if dry_run:
    stats[MODEL_REASON] = len(junk)
    return stats
  stats[MODEL_REASON] = _set_reason(conn, junk, MODEL_REASON)
  conn.commit()
  return stats


def annotate_on_ingest(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, Any] | None:
  """Post-ingest junk pass from config `quality`: mechanical rules, then the model."""
  q = cfg.get("quality") or {}
  if not q.get("annotate_on_ingest"):
    return None
  out: dict[str, Any] = {"ran": True, **annotate_mechanical(conn)}
  model_cfg = q.get("junk_model") or {}
  if model_cfg.get("enabled"):
    out["model"] = annotate_model(conn, model_cfg)
  return out
