from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc


MIN_CONTENT_CHARS = 20


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

      # body already contains the statement section; prepend bare statement
      # only if body doesn't open with it, so it surfaces prominently in FTS
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
