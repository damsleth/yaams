from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc

MIN_CONTENT_CHARS = 20


def index_path_for(block: dict) -> Path | None:
  """note_index.json for an `ingest.tier2_ledger` config block, or None."""
  notes_path = block.get("notes_path")
  if not notes_path:
    return None
  return expand_path(block.get("index_path") or str(Path(notes_path) / "08_indices" / "note_index.json"))


def live_ids(index_path: Path) -> frozenset[str] | None:
  """Item ids of every note in the current index; None if unreadable or empty.

  The ledger drops archived notes from its index, but yaams never deletes rows,
  so this is the only signal that a stored tier2 note is no longer live."""
  try:
    return _live_ids(index_path, index_path.stat().st_mtime)
  except (OSError, ValueError):
    return None


@lru_cache(maxsize=2)
def _live_ids(index_path: Path, _mtime: float) -> frozenset[str] | None:
  entries = json.loads(index_path.read_text(encoding="utf-8")).get("entries") or {}
  ids = frozenset(
    hash_id("tier2_ledger", (e.get("candidate") or {}).get("rel_path") or key)
    for key, e in entries.items()
  )
  return ids or None


@dataclass
class LedgerNotesAdapter:
  notes_path: Path
  index_path: Path
  skipped_empty: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_empty = 0
    cutoff = ensure_utc(since)
    index_file = expand_path(self.index_path)
    index = json.loads(index_file.read_text(encoding="utf-8"))
    entries = index.get("entries", {})

    for _key, entry in entries.items():
      mtime = datetime.fromtimestamp(float(entry["mtime"]), tz=UTC)
      if mtime < cutoff:
        continue

      candidate = entry.get("candidate") or {}
      body = (candidate.get("body") or "").strip()
      statement = (candidate.get("statement") or "").strip()

      # ponytail: deliberate Tier 2 lexical weight. Real bodies open
      # "# Title\n\n## Statement", so this check almost never matches and the
      # statement is indexed twice. Removing the doubling was measured and lost
      # (experiments.jsonl `ledger_statement_dedup`, 2026-09-25: dev quality
      # -0.023, one rank-1 loss). Revisit only with >=10 tier2 golds.
      if statement and not body.lstrip("#\n ").startswith(statement[:40]):
        content = statement + "\n\n" + body
      else:
        content = body

      if len(content) < MIN_CONTENT_CHARS:
        self.skipped_empty += 1
        continue

      rel_path = candidate.get("rel_path") or _key

      yield Item(
        id=hash_id("tier2_ledger", rel_path),
        source="tier2_ledger",
        source_id=rel_path,
        timestamp=mtime,
        sender="me",
        recipients=[],
        content=content,
        subject=candidate.get("title") or None,
        thread_id=candidate.get("type") or None,
        raw_metadata={
          "note_type": entry.get("note_type"),
          "content_hash": entry.get("content_hash"),
          "path": candidate.get("path"),
        },
      )
