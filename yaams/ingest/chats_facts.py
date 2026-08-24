from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest._markdown import (
  parse_frontmatter,
  subject_from,
  walk_markdown,
)
from yaams.ingest.base import Item, hash_id
from yaams.ingest.chats import (
  DEFAULT_SKIP_DIRS,
  _chat_lang,
  _chat_timestamp,
)
from yaams.time import ensure_utc

# The capture-chat.sh SessionEnd hook already distils each session into atomic,
# decision-centric bullets under a `## Insights / Facts` heading (94/138 files
# at last count). This adapter promotes each bullet to its own tiny item so a
# query matches the single relevant fact instead of competing against a 5-10k
# char summary. `facts_from_file` is a pure `file -> [FactRecord]` extractor so
# the ledger-promotion sink can consume the same facts without going through the
# raw store.

# ponytail: only `## Insights / Facts` for now. `## Open loops` (86 files) maps
# onto cognitive-ledger's open-loop note type and is the natural next section to
# add once fact-indexing proves out. The 44 files without the section keep
# whole-doc indexing via the `chats` source — no fallback extraction here.
_FACTS_SECTION = "Insights / Facts"
MIN_FACT_CHARS = 20

_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")


@dataclass
class FactRecord:
  source_id: str  # "<relpath>#<sha8(bullet)>" — stable across re-ingest
  content: str
  subject: str | None
  tags: list[str]
  timestamp: datetime
  timestamp_inferred: bool
  session_id: str | None
  lang: str | None
  path: str


def _extract_section(raw: str, section: str) -> str | None:
  """Return the text under `## <section>` up to the next `## ` heading (or EOF),
  or None if the section is absent."""
  lines = raw.splitlines()
  start: int | None = None
  target = f"## {section}".lower()
  for i, line in enumerate(lines):
    if line.strip().lower() == target:
      start = i + 1
      break
  if start is None:
    return None
  body: list[str] = []
  for line in lines[start:]:
    if line.startswith("## "):
      break
    body.append(line)
  return "\n".join(body)


def _parse_bullets(section_text: str) -> list[str]:
  """Split a section into fact bullets. Bullets are single-line `- ` entries in
  practice; a non-bullet, non-blank line is folded into the current bullet as a
  defensive continuation. Bullets shorter than MIN_FACT_CHARS are dropped."""
  bullets: list[str] = []
  current: str | None = None
  for line in section_text.splitlines():
    stripped = line.strip()
    m = _BULLET_RE.match(stripped)
    if m:
      if current is not None:
        bullets.append(current.strip())
      current = m.group(1)
    elif not stripped:
      if current is not None:
        bullets.append(current.strip())
        current = None
    elif current is not None:
      current += " " + stripped
  if current is not None:
    bullets.append(current.strip())
  return [b for b in bullets if len(b) >= MIN_FACT_CHARS]


def facts_from_file(md_file: Path, chats_root: Path) -> list[FactRecord]:
  """Pure extractor: parse a chat summary's `## Insights / Facts` bullets into
  FactRecords. Empty list if the file has no such section or no usable bullets."""
  raw = md_file.read_text(encoding="utf-8", errors="replace")
  section = _extract_section(raw, _FACTS_SECTION)
  if not section:
    return []
  bullets = _parse_bullets(section)
  if not bullets:
    return []

  fm = parse_frontmatter(raw)
  mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=UTC)
  timestamp, inferred = _chat_timestamp(fm, md_file, mtime)
  subject = subject_from(fm, raw, md_file)
  lang = _chat_lang(fm)
  raw_tags = fm.get("tags")
  tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
  session_id = fm.get("session_id") or None
  rel = str(md_file.relative_to(chats_root))

  records: list[FactRecord] = []
  for bullet in bullets:
    digest = sha256(bullet.encode("utf-8")).hexdigest()[:8]
    records.append(
      FactRecord(
        source_id=f"{rel}#{digest}",
        content=bullet,
        subject=subject,
        tags=tags,
        timestamp=timestamp,
        timestamp_inferred=inferred,
        session_id=session_id,
        lang=lang,
        path=rel,
      )
    )
  return records


@dataclass
class ChatsFactsAdapter:
  chats_path: Path
  skip_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_SKIP_DIRS))
  skip_filename_prefixes: tuple[str, ...] = ("_", ".")

  def extract(self, since: datetime) -> Iterator[Item]:
    chats = expand_path(self.chats_path)
    cutoff = ensure_utc(since)
    if not chats.exists():
      return

    for md_file in walk_markdown(chats, self.skip_dirs, self.skip_filename_prefixes):
      mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue
      for fact in facts_from_file(md_file, chats):
        yield Item(
          id=hash_id("chats_facts", fact.source_id),
          source="chats_facts",
          source_id=fact.source_id,
          timestamp=fact.timestamp,
          timestamp_inferred=fact.timestamp_inferred,
          sender="me",
          recipients=[],
          content=fact.content,
          subject=fact.subject,
          thread_id=fact.session_id,
          lang=fact.lang,
          raw_metadata={
            "path": fact.path,
            "session_id": fact.session_id,
            "tags": fact.tags,
            "fact": True,
          },
        )
