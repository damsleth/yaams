"""Scan-and-judge review queue for query feedback.

Mirrors the cognitive-ledger ``review`` pattern but operates on logged
queries rather than notes. The queue prioritizes queries that are
unjudged, ambiguous (many results), or low-confidence — i.e. the ones
most worth a verdict. Verdicts map a single keystroke to a feedback
kind so a TUI can drive the loop without typing query ids.

All feedback I/O routes through :func:`yaams.signals.logger.log_feedback`.
This module is pure (modulo read-only DB reads in :func:`build_review_queue`
and :func:`dashboard_data`) so it can be tested without curses.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from yaams.signals.logger import log_feedback

VERDICT_KINDS = {"hit", "miss", "correction", "noise", "relevant", "thin"}
"""Feedback kinds the review loop can emit.

Answer-shaped queries (factual, first/last_occurrence, event_anchored) have
one pointable right row, so they grade answer *precision*:

- ``hit`` / ``miss`` / ``correction``: graded — counted in hit-rate stats.

Recall-shaped queries (synthesis, temporal_range) ask for a useful *set*, not
a single answer, so "which rank is right" doesn't apply. They grade set
*usefulness* instead:

- ``relevant`` / ``thin``: graded — counted in the usefulness rate. ``relevant``
  means the result set was useful context; ``thin`` means too sparse/off-topic
  to help. Kept out of the answer hit-rate so the two axes don't conflate.

Shared:

- ``noise``: the query had no real intent (probe, test, placeholder).
  Excluded from both rates but still counts as "judged" so it drops out of the
  review queue. Mark with ``n`` in the TUI.
"""

# Shapes (from yaams.retrieve.parse) that ask for a useful *set* rather than a
# single answer. Everything else is treated as answer-shaped.
RECALL_SHAPES = frozenset({"synthesis", "temporal_range"})


def is_answer_shaped(shape: str | None, parser_fallback: bool = False) -> bool:
  """True if a query expects one right answer (hit/miss/correction grading).

  False — i.e. grade set usefulness (relevant/thin) — when either:

  - the shape is a recall shape (synthesis, temporal_range), or
  - ``parser_fallback`` is set, meaning the parser never understood the
    query. On a dummy/absent LLM backend *every* query falls back to a
    placeholder ``shape="factual"`` (see ``retrieve.parse._fallback``), so the
    stored shape is meaningless — we can't claim there's a single right row,
    and the honest grade is "was the result set useful?".

  A confidently-parsed ``factual`` query (fallback False) stays answer-shaped.
  """
  if parser_fallback:
    return False
  return (shape or "").strip().lower() not in RECALL_SHAPES


_SNIPPET_LEN = 480

_HELP_LINES_ANSWER = [
  "h  hit (top result was right)      m  miss (none right)",
  "1-9  correction (that rank is the right answer)",
  "n  noise (no real intent — cascades to identical text)",
  "space/enter  skip                  u  undo last      q  quit & save",
  "up/down  scroll results            ?  toggle this help",
]

_HELP_LINES_RECALL = [
  "r  relevant (useful result set)    t  thin (too sparse/off to help)",
  "recall-shaped query — no single 'right' answer, so grade the set",
  "n  noise (no real intent — cascades to identical text)",
  "space/enter  skip                  u  undo last      q  quit & save",
  "up/down  scroll results            ?  toggle this help",
]


def _help_lines(item: "ReviewItem") -> list[str]:
  if is_answer_shaped(item.shape, item.parser_fallback):
    return _HELP_LINES_ANSWER
  return _HELP_LINES_RECALL


@dataclass
class ReviewResult:
  """One ranked result inside a :class:`ReviewItem`."""

  rank: int
  result_id: str
  kind: str
  source: str | None
  rrf_score: float | None
  snippet: str
  sender: str | None
  timestamp: str | None
  cited: bool


@dataclass
class ReviewItem:
  """One query queued for review, with its ranked results and priority."""

  query_id: str
  text: str
  ts: str
  results_returned: int
  shape: str | None
  confidence: str | None
  cited_count: int
  results: list[ReviewResult] = field(default_factory=list)
  priority: float = 0.0
  reasons: list[str] = field(default_factory=list)
  parser_fallback: bool = False

  @property
  def reason(self) -> str:
    return " · ".join(self.reasons) if self.reasons else "routine review"


# ---------------------------------------------------------------------------
# Queue construction
# ---------------------------------------------------------------------------


def _age_days(ts_iso: str, now: datetime) -> float:
  s = (ts_iso or "").strip().replace("Z", "+00:00")
  try:
    d = datetime.fromisoformat(s)
  except ValueError:
    return float("inf")
  if d.tzinfo is None:
    d = d.replace(tzinfo=UTC)
  return (now - d).total_seconds() / 86400.0


def score_query(
  *,
  unjudged: bool,
  results_returned: int,
  confidence: str | None,
  cited_count: int,
  age_days: float,
) -> tuple[float, list[str]]:
  """Compute a priority and human-readable reasons for one queue item.

  Pure function — exposed for tests and external callers that want to
  re-rank a queue with custom inputs.
  """
  priority = 0.0
  reasons: list[str] = []
  if unjudged:
    priority += 1.0
    reasons.append("unjudged")
  if (confidence or "").lower() == "low":
    priority += 0.5
    reasons.append("low-confidence answer")
  if results_returned >= 5:
    priority += 0.3
    reasons.append(f"{results_returned} results — ambiguous")
  elif results_returned == 0:
    priority += 0.4
    reasons.append("zero results")
  if cited_count == 0 and results_returned > 0:
    priority += 0.2
    reasons.append("answer cited no results")
  if age_days < 1.0:
    priority += 0.4
    reasons.append("from today")
  elif age_days < 7.0:
    priority += 0.2
  return priority, reasons


def _snippet_for(
  conn: sqlite3.Connection, *, result_id: str, kind: str
) -> tuple[str, str | None, str | None]:
  """Return (snippet, sender, timestamp) for one result. None on missing.

  Consolidation snippets are run through
  :func:`yaams.render.render_consolidation_snippet` so the stored header
  and per-line prefixes don't waste display real estate. The returned
  snippet may contain newlines — callers should split on them before
  wrapping."""
  from yaams.render import render_consolidation_snippet, short_participants

  if kind == "consolidation":
    row = conn.execute(
      "SELECT summary, participants, start_timestamp FROM consolidations WHERE id = ?",
      (result_id,),
    ).fetchone()
    if row is None:
      return ("", None, None)
    text = (row["summary"] if hasattr(row, "keys") else row[0]) or ""
    raw_participants = (
      row["participants"] if hasattr(row, "keys") else row[1]
    ) or None
    ts = (row["start_timestamp"] if hasattr(row, "keys") else row[2]) or None
    snippet = render_consolidation_snippet(
      str(text), multiline=True, max_chars=_SNIPPET_LEN
    )
    sender: str | None = None
    if raw_participants:
      try:
        parsed = json.loads(raw_participants)
        if isinstance(parsed, list):
          sender = short_participants(parsed)
        else:
          sender = str(raw_participants)
      except (TypeError, ValueError):
        sender = str(raw_participants)
    return (snippet, sender, ts)

  row = conn.execute(
    "SELECT content, sender, timestamp FROM items WHERE id = ?",
    (result_id,),
  ).fetchone()
  if row is None:
    return ("", None, None)
  text = (row["content"] if hasattr(row, "keys") else row[0]) or ""
  sender = (row["sender"] if hasattr(row, "keys") else row[1]) or None
  ts = (row["timestamp"] if hasattr(row, "keys") else row[2]) or None
  snippet = " ".join(str(text).split())
  if len(snippet) > _SNIPPET_LEN:
    snippet = snippet[: _SNIPPET_LEN - 1] + "…"
  return (snippet, sender, ts)


def build_review_queue(
  conn: sqlite3.Connection,
  *,
  since: str | None = None,
  source: str | None = None,
  limit: int | None = None,
  unjudged_only: bool = True,
  top_results: int = 5,
  now: datetime | None = None,
) -> list[ReviewItem]:
  """Build a priority-ordered review queue from the ``queries`` table.

  Args:
    since: ISO timestamp; only include queries logged at/after this.
    source: Restrict to queries whose ``source_filter`` JSON contains this
      source. Substring match — coarse but enough for v1.
    limit: Cap the queue length after sorting.
    unjudged_only: Skip queries that already have any ``query_feedback`` row.
    top_results: How many ranked results to attach per query.
    now: Override "now" for deterministic tests.
  """
  now = now or datetime.now(UTC)

  where: list[str] = []
  params: list[Any] = []
  if since:
    where.append("q.ts >= ?")
    params.append(since)
  if source:
    where.append("q.source_filter LIKE ?")
    params.append(f"%{source}%")
  if unjudged_only:
    where.append("NOT EXISTS (SELECT 1 FROM query_feedback f WHERE f.query_id = q.id)")

  sql = """
    SELECT
      q.id, q.text, q.ts, q.results_returned,
      q.shape, q.confidence, q.parser_fallback
    FROM queries AS q
  """
  if where:
    sql += " WHERE " + " AND ".join(where)
  sql += " ORDER BY q.ts DESC"

  rows = conn.execute(sql, params).fetchall()

  queue: list[ReviewItem] = []
  for row in rows:
    qid = row["id"] if hasattr(row, "keys") else row[0]
    text = row["text"] if hasattr(row, "keys") else row[1]
    ts = row["ts"] if hasattr(row, "keys") else row[2]
    results_returned = row["results_returned"] if hasattr(row, "keys") else row[3]
    shape = row["shape"] if hasattr(row, "keys") else row[4]
    confidence = row["confidence"] if hasattr(row, "keys") else row[5]
    parser_fallback = bool(row["parser_fallback"] if hasattr(row, "keys") else row[6])

    result_rows = conn.execute(
      """
      SELECT rank, result_id, kind, source, rrf_score, cited
      FROM query_results
      WHERE query_id = ?
      ORDER BY rank ASC
      LIMIT ?
      """,
      (qid, top_results),
    ).fetchall()
    results: list[ReviewResult] = []
    cited_count_top = 0
    for rr in result_rows:
      rid = rr["result_id"] if hasattr(rr, "keys") else rr[1]
      rkind = rr["kind"] if hasattr(rr, "keys") else rr[2]
      snippet, sender, rts = _snippet_for(conn, result_id=rid, kind=rkind)
      cited = bool(rr["cited"] if hasattr(rr, "keys") else rr[5])
      if cited:
        cited_count_top += 1
      results.append(
        ReviewResult(
          rank=int(rr["rank"] if hasattr(rr, "keys") else rr[0]),
          result_id=rid,
          kind=rkind,
          source=rr["source"] if hasattr(rr, "keys") else rr[3],
          rrf_score=rr["rrf_score"] if hasattr(rr, "keys") else rr[4],
          snippet=snippet,
          sender=sender,
          timestamp=rts,
          cited=cited,
        )
      )

    # `cited_count` for scoring uses the full result set, not just the top
    # slice. A query that cited rank-7 should not look "unanchored" because
    # we only fetched the top 5 for display.
    cited_full = conn.execute(
      "SELECT COUNT(*) FROM query_results WHERE query_id = ? AND cited = 1",
      (qid,),
    ).fetchone()
    cited_count = int(cited_full[0] if cited_full else 0)

    priority, reasons = score_query(
      unjudged=True if unjudged_only else _is_unjudged(conn, qid),
      results_returned=int(results_returned or 0),
      confidence=confidence,
      cited_count=cited_count,
      age_days=_age_days(ts, now),
    )
    queue.append(
      ReviewItem(
        query_id=qid,
        text=text,
        ts=ts,
        results_returned=int(results_returned or 0),
        shape=shape,
        confidence=confidence,
        cited_count=cited_count,
        results=results,
        priority=priority,
        reasons=reasons,
        parser_fallback=parser_fallback,
      )
    )

  queue.sort(key=lambda it: (it.priority, it.ts), reverse=True)
  if limit is not None:
    queue = queue[:limit]
  return queue


def _is_unjudged(conn: sqlite3.Connection, query_id: str) -> bool:
  row = conn.execute(
    "SELECT 1 FROM query_feedback WHERE query_id = ? LIMIT 1", (query_id,)
  ).fetchone()
  return row is None


# ---------------------------------------------------------------------------
# Verdict mapping + session flush
# ---------------------------------------------------------------------------


def verdict_signal(item: ReviewItem, key: str) -> dict[str, Any] | None:
  """Map one keystroke to :func:`log_feedback` kwargs, or None to skip.

  The accepted keys depend on the query's shape (see :func:`is_answer_shaped`).

  Shared:
    - ``n`` → ``noise`` (no real intent — probe, test, placeholder).
      In the TUI this also cascades to identical-text unjudged queries
      via :func:`noise_cascade`; this function only emits the single row.

  Answer-shaped queries (factual, first/last_occurrence, event_anchored):
    - ``h`` → ``hit`` on the top-1 result (the common case).
    - ``m`` → ``miss`` (no useful results).
    - ``1``..``9`` → ``correction`` naming the result at that rank as the
      right answer. Returns None if no result at that rank.

  Recall-shaped queries (synthesis, temporal_range):
    - ``r`` → ``relevant`` (the result set was useful context).
    - ``t`` → ``thin`` (results too sparse/off-topic to help).

  Keys that don't apply to the query's shape — and anything else (space,
  enter, ``q``, etc.) — return None.
  """
  if not key:
    return None
  if key == "n":
    return {"query_id": item.query_id, "kind": "noise"}

  if is_answer_shaped(item.shape, item.parser_fallback):
    if key == "h":
      if not item.results:
        return None
      return {
        "query_id": item.query_id,
        "kind": "hit",
        "result_id": item.results[0].result_id,
      }
    if key == "m":
      return {"query_id": item.query_id, "kind": "miss"}
    if len(key) == 1 and key in "123456789":
      rank = int(key)
      target = next((r for r in item.results if r.rank == rank), None)
      if target is None:
        return None
      return {
        "query_id": item.query_id,
        "kind": "correction",
        "result_id": target.result_id,
      }
    return None

  # Recall-shaped: grade set usefulness, not a single answer.
  if key == "r":
    return {"query_id": item.query_id, "kind": "relevant"}
  if key == "t":
    return {"query_id": item.query_id, "kind": "thin"}
  return None


def noise_cascade(
  conn: sqlite3.Connection, *, query_id: str, text: str
) -> list[dict[str, Any]]:
  """Return ``log_feedback`` kwargs to mark *every* unjudged query with the
  same text as noise — including ``query_id`` itself.

  The cascade catches the common case of a probe string ("anything",
  "test", a placeholder paste) that was issued repeatedly. The caller
  passes the returned entries to :func:`flush_session`.

  Cascade is intentionally exact-match on ``queries.text``. Fuzzy
  matching would risk false positives that erase real signal.
  """
  rows = conn.execute(
    """
    SELECT q.id
    FROM queries AS q
    WHERE q.text = ?
      AND NOT EXISTS (
        SELECT 1 FROM query_feedback f WHERE f.query_id = q.id
      )
    """,
    (text,),
  ).fetchall()
  ids = [r[0] if not hasattr(r, "keys") else r["id"] for r in rows]
  # Always include the seed query, even if (somehow) it already has feedback.
  if query_id not in ids:
    ids.append(query_id)
  return [{"query_id": qid, "kind": "noise"} for qid in ids]


def flush_session(
  conn: sqlite3.Connection, entries: list[dict[str, Any]]
) -> int:
  """Write buffered verdict entries via :func:`log_feedback`.

  Returns the number of rows written. Each entry must already be a valid
  ``log_feedback`` kwargs dict (as produced by :func:`verdict_signal`).
  """
  count = 0
  for kwargs in entries:
    log_feedback(conn, **kwargs)
    count += 1
  return count


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def dashboard_data(conn: sqlite3.Connection) -> dict[str, Any]:
  """Aggregate coverage and verdict-mix stats from the queries tables."""
  total = int(
    conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] or 0
  )
  judged = int(
    conn.execute(
      "SELECT COUNT(DISTINCT query_id) FROM query_feedback"
    ).fetchone()[0]
    or 0
  )
  by_kind_rows = conn.execute(
    "SELECT kind, COUNT(*) FROM query_feedback GROUP BY kind"
  ).fetchall()
  by_kind = {
    (r[0] if not hasattr(r, "keys") else r["kind"]): int(
      r[1] if not hasattr(r, "keys") else r["COUNT(*)"]
    )
    for r in by_kind_rows
  }

  hit = by_kind.get("hit", 0)
  miss = by_kind.get("miss", 0)
  correction = by_kind.get("correction", 0)
  noise = by_kind.get("noise", 0)
  relevant = by_kind.get("relevant", 0)
  thin = by_kind.get("thin", 0)
  # Answer-shaped grading: did the right row come back / rank well?
  graded = hit + miss + correction
  hit_rate = (hit / graded) if graded else 0.0
  # Recall-shaped grading: was the result set useful? Separate axis — kept
  # out of hit_rate so set-usefulness doesn't dilute answer precision.
  graded_recall = relevant + thin
  usefulness_rate = (relevant / graded_recall) if graded_recall else 0.0

  miss_sources_rows = conn.execute(
    """
    SELECT qr.source, COUNT(*) AS n
    FROM query_feedback f
    JOIN query_results qr ON qr.query_id = f.query_id AND qr.rank = 1
    WHERE f.kind = 'miss' AND qr.source IS NOT NULL
    GROUP BY qr.source
    ORDER BY n DESC
    LIMIT 5
    """
  ).fetchall()
  miss_sources = {
    (r[0] if not hasattr(r, "keys") else r["source"]): int(
      r[1] if not hasattr(r, "keys") else r["n"]
    )
    for r in miss_sources_rows
  }

  # Provenance breakdown — surfaces how many rows came from tests /
  # the CLI / unknown sources, so noisy buckets stand out at a glance.
  prov_rows = conn.execute(
    "SELECT COALESCE(provenance, 'unknown') AS p, COUNT(*) AS n "
    "FROM queries GROUP BY p ORDER BY n DESC"
  ).fetchall()
  by_provenance = {
    (r[0] if not hasattr(r, "keys") else r["p"]): int(
      r[1] if not hasattr(r, "keys") else r["n"]
    )
    for r in prov_rows
  }

  return {
    "total_queries": total,
    "judged_queries": judged,
    "coverage": (judged / total) if total else 0.0,
    "by_kind": by_kind,
    "hit_rate": hit_rate,
    "graded_queries": graded,
    "usefulness_rate": usefulness_rate,
    "graded_recall_queries": graded_recall,
    "noise_queries": noise,
    "miss_sources": miss_sources,
    "by_provenance": by_provenance,
  }


# ---------------------------------------------------------------------------
# Curses TUI — thin shell over the pure layer above
# ---------------------------------------------------------------------------


def run_review_tui(
  conn: sqlite3.Connection, queue: list[ReviewItem]
) -> dict[str, Any]:
  """Drive ``queue`` interactively in curses, flushing verdicts on exit.

  Returns a session summary: ``{judged, entries}``. The curses loop runs
  inside :func:`curses.wrapper` so the terminal is restored even on
  exceptions. ``Ctrl-C`` exits without flushing — matches the ledger's
  contract that a clean ``q`` is the only path that writes signals.
  """
  if not queue:
    print("Review queue is empty — nothing to judge.")
    return {"judged": 0, "entries": []}

  try:
    import curses
  except ImportError as exc:  # pragma: no cover - platform dependent
    raise RuntimeError(
      "The review TUI requires the stdlib 'curses' module, which is "
      "unavailable on this platform. Use `yaams review --queue` or "
      "`--stats` instead."
    ) from exc

  entries: list[dict[str, Any]] = []
  try:
    curses.wrapper(_review_loop, queue, entries, conn)
  except KeyboardInterrupt:
    # Match ledger: Ctrl-C bails without flushing.
    return {"judged": 0, "entries": [], "aborted": True}

  written = flush_session(conn, entries)
  return {"judged": written, "entries": entries}


def _review_loop(stdscr, queue, entries, conn):  # pragma: no cover - curses UI
  import curses

  curses.curs_set(0)
  stdscr.keypad(True)
  # Inherit the terminal's fg/bg colors so the TUI doesn't paint its own
  # black background on light themes. `use_default_colors` rebinds color
  # pair 0 to (default_fg, default_bg); we then set the window's bkgd
  # character so `erase()` paints with that pair instead of curses' own
  # white-on-black default. Wrapped in try/except because not every
  # terminal supports colors (e.g. when TERM=dumb).
  try:
    curses.start_color()
    curses.use_default_colors()
    stdscr.bkgd(" ", curses.color_pair(0))
  except curses.error:
    pass
  idx = 0
  scroll = 0
  show_help = False
  # history items track how many entries were appended so undo can pop the
  # correct number (noise cascade can append many at once).
  history: list[tuple[int, int]] = []
  # Track in-session noise cascades by text so we don't re-cascade the same
  # text on every subsequent card with that text.
  cascaded_texts: set[str] = set()
  flash = ""

  while idx < len(queue):
    item = queue[idx]
    _draw_card(stdscr, item, idx, len(queue), len(entries), scroll, show_help, flash)
    flash = ""
    ch = stdscr.getch()

    if ch == curses.KEY_RESIZE:
      continue
    if ch == ord("q"):
      break
    if ch == ord("?"):
      show_help = not show_help
      continue
    if ch in (curses.KEY_DOWN, ord("J")):
      scroll += 1
      continue
    if ch in (curses.KEY_UP, ord("K")):
      scroll = max(0, scroll - 1)
      continue
    if ch == ord("u"):
      if history:
        prev_idx, count = history.pop()
        for _ in range(count):
          if entries:
            entries.pop()
        idx = prev_idx
        scroll = 0
        flash = f"undid last verdict ({count} row{'s' if count != 1 else ''})"
      else:
        flash = "nothing to undo"
      continue
    if ch in (ord(" "), 10, 13):  # space / enter — skip
      history.append((idx, 0))
      idx += 1
      scroll = 0
      continue

    key = chr(ch) if 0 <= ch < 256 else ""

    if key == "n":
      text = item.text or ""
      if text in cascaded_texts:
        # Already cascaded this text — just mark this single row.
        entry = {"query_id": item.query_id, "kind": "noise"}
        entries.append(entry)
        history.append((idx, 1))
        flash = "noise (already cascaded earlier in session)"
      else:
        cascade = noise_cascade(conn, query_id=item.query_id, text=text)
        # Filter out anything already buffered to avoid double-logging in
        # this session (e.g. the seed query was added earlier).
        already = {e["query_id"] for e in entries}
        fresh = [e for e in cascade if e["query_id"] not in already]
        entries.extend(fresh)
        cascaded_texts.add(text)
        history.append((idx, len(fresh)))
        flash = f"noise — cascaded {len(fresh)} row(s) with identical text"
      idx += 1
      scroll = 0
      continue

    entry = verdict_signal(item, key)
    if entry is None:
      answer_shaped = is_answer_shaped(item.shape, item.parser_fallback)
      keys = "h/m/n, 1-9" if answer_shaped else "r/t/n"
      flash = f"'{key}' — no verdict ({keys}, space=skip, ? help)"
      continue
    entries.append(entry)
    history.append((idx, 1))
    idx += 1
    scroll = 0


def _draw_card(stdscr, item, idx, total, judged, scroll, show_help, flash):  # pragma: no cover
  import curses
  import textwrap

  stdscr.erase()
  height, width = stdscr.getmaxyx()
  w = max(20, width - 2)

  def line(y, text, attr=0):
    if 0 <= y < height:
      try:
        stdscr.addnstr(y, 1, text, w, attr)
      except curses.error:
        pass

  line(0, f"yaams review   {total - idx} left · {judged} judged", curses.A_BOLD)
  line(1, "─" * w)

  q_text = (item.text or "").replace("\n", " ").strip()
  line(2, f"Q: {q_text}", curses.A_BOLD)

  meta_bits = [item.ts]
  # A fallback query's stored shape is a placeholder ("factual"), not a real
  # finding — show it as "unparsed" so the recall-style verdict keys make sense.
  if item.parser_fallback:
    meta_bits.append("shape unparsed")
  elif item.shape:
    meta_bits.append(f"shape {item.shape}")
  if item.confidence:
    meta_bits.append(f"conf {item.confidence}")
  meta_bits.append(f"results {item.results_returned}")
  if item.cited_count:
    meta_bits.append(f"★ {item.cited_count} cited")
  line(3, "  ·  ".join(str(b) for b in meta_bits))
  line(4, f"▸ {item.reason}", curses.A_DIM)
  line(5, "─" * w)

  help_lines = _help_lines(item)
  body_top = 6
  footer_rows = 3 + (len(help_lines) if show_help else 0)
  body_height = max(1, height - body_top - footer_rows)

  # Render each result as a block: header + wrapped snippet lines.
  # Snippets may contain newlines (from render_consolidation_snippet);
  # preserve them by wrapping each line independently.
  rendered: list[tuple[str, int]] = []  # (text, attr-flag: 0 normal, 1 dim, 2 bold)
  for r in item.results:
    cited = "★" if r.cited else " "
    src = r.source or "-"
    ts_short = (r.timestamp or "")[:10]
    header = f" {cited} {r.rank}. [{src}] {ts_short}  {r.sender or ''}".rstrip()
    rendered.append((header, 2))
    snippet_lines = (r.snippet or "(no snippet)").splitlines() or [""]
    for snippet_line in snippet_lines:
      wrapped_lines = textwrap.wrap(
        snippet_line, w - 4, subsequent_indent="  "
      ) or [""]
      for wrapped in wrapped_lines:
        rendered.append(("    " + wrapped, 1))
    rendered.append(("", 0))

  visible = rendered[scroll : scroll + body_height]
  for i, (text, attr_flag) in enumerate(visible):
    attr = 0
    if attr_flag == 1:
      attr = curses.A_DIM
    elif attr_flag == 2:
      attr = curses.A_BOLD
    line(body_top + i, text, attr)
  if scroll + body_height < len(rendered):
    line(body_top + body_height - 1, "… (↓ for more)", curses.A_DIM)

  foot = height - footer_rows
  line(foot, "─" * w)
  if is_answer_shaped(item.shape, item.parser_fallback):
    keybar = "[h]it  [m]iss  [1-9]correction  [n]oise  [space]skip  [u]ndo  [q]uit  [?]help"
  else:
    keybar = "[r]elevant  [t]hin  [n]oise  [space]skip  [u]ndo  [q]uit  [?]help"
  line(foot + 1, keybar, curses.A_BOLD)
  if flash:
    line(foot + 2, flash, curses.A_REVERSE)
  if show_help:
    for i, htext in enumerate(help_lines):
      line(foot + 3 + i, htext, curses.A_DIM)
  stdscr.refresh()


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------


def render_dashboard(data: dict[str, Any]) -> str:
  """Format :func:`dashboard_data` as a plain-text block."""
  lines: list[str] = []
  lines.append("Query feedback dashboard")
  lines.append("=" * 40)
  lines.append(f"Total queries : {data['total_queries']}")
  coverage_pct = data["coverage"] * 100
  lines.append(
    f"Coverage      : {data['judged_queries']}/{data['total_queries']} "
    f"queries judged ({coverage_pct:.0f}%)"
  )
  if data["graded_queries"]:
    lines.append(
      f"Hit rate      : {data['hit_rate'] * 100:.0f}% "
      f"(of {data['graded_queries']} graded, answer-shaped)"
    )
  if data.get("graded_recall_queries"):
    lines.append(
      f"Usefulness    : {data['usefulness_rate'] * 100:.0f}% "
      f"(of {data['graded_recall_queries']} graded, recall-shaped)"
    )
  if data.get("noise_queries"):
    lines.append(f"Noise         : {data['noise_queries']} (excluded from hit rate)")
  by_kind = data.get("by_kind") or {}
  if by_kind:
    lines.append("")
    lines.append("By kind:")
    for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
      lines.append(f"  {count:4d}  {kind}")
  miss_sources = data.get("miss_sources") or {}
  if miss_sources:
    lines.append("")
    lines.append("Top miss sources (top-1 was wrong):")
    for src, n in miss_sources.items():
      lines.append(f"  {n:4d}  {src}")
  by_provenance = data.get("by_provenance") or {}
  if by_provenance:
    lines.append("")
    lines.append("By provenance:")
    for prov, n in by_provenance.items():
      lines.append(f"  {n:4d}  {prov}")
  return "\n".join(lines)
