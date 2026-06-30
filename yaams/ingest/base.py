from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Iterator, Optional, Protocol

from yaams.time import utc_now


@dataclass(frozen=True)
class Item:
  id: str
  source: str
  source_id: str
  timestamp: datetime
  sender: str
  recipients: list[str]
  content: str
  subject: Optional[str] = None
  thread_id: Optional[str] = None
  lang: Optional[str] = None
  raw_metadata: dict = field(default_factory=dict)
  ingested_at: datetime = field(default_factory=utc_now)
  # True when ``timestamp`` was a fallback (e.g. file mtime) rather than a real
  # date signal from the content. Recency-sorted retrieval excludes these so
  # undated items don't masquerade as the freshest in the corpus.
  timestamp_inferred: bool = False


class Adapter(Protocol):
  def extract(self, since: datetime) -> Iterator[Item]:
    ...


def hash_id(source: str, source_id: str) -> str:
  # Deterministic, content-stable id: the same logical item always hashes the
  # same, and mutable sources encode their revision in source_id so a change is
  # a new id, not a rewrite. See AGENTS.md "Raw-store invariants".
  return sha256(f"{source}:{source_id}".encode("utf-8")).hexdigest()

