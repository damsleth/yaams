from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest._markdown import (
  MIN_CONTENT_CHARS,
  collapse_blank_lines,
  date_from_filename,
  dated_frontmatter_value,
  parse_frontmatter,
  strip_frontmatter,
  subject_from,
  walk_markdown,
)
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc

# Claude Code session summaries written by the capture-chat.sh SessionEnd hook.
# One markdown file per session, flat in the chats dir, with YAML frontmatter
# carrying created/updated/session_id/cwd/git_branch/model/tags/title/lang.

# Frontmatter keys carrying a creation date, in priority order. This differs
# from obsidian's order on purpose: a session summary's `created` is
# authoritative where a note's `date` is.
_FM_DATE_KEYS = ("created", "created_at", "date", "updated")

DEFAULT_SKIP_DIRS = {".git", ".obsidian", ".claude"}


@dataclass
class ChatsAdapter:
  chats_path: Path
  skip_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_SKIP_DIRS))
  skip_filename_prefixes: tuple[str, ...] = ("_", ".")
  skipped_empty: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_empty = 0
    chats = expand_path(self.chats_path)
    cutoff = ensure_utc(since)

    if not chats.exists():
      return

    for md_file in walk_markdown(chats, self.skip_dirs, self.skip_filename_prefixes):
      mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue

      raw = md_file.read_text(encoding="utf-8", errors="replace")
      frontmatter = parse_frontmatter(raw)
      content = collapse_blank_lines(strip_frontmatter(raw))

      if len(content) < MIN_CONTENT_CHARS:
        self.skipped_empty += 1
        continue

      rel = md_file.relative_to(chats)
      source_id = str(rel)
      timestamp, inferred = _chat_timestamp(frontmatter, md_file, mtime)

      yield Item(
        id=hash_id("chats", source_id),
        source="chats",
        source_id=source_id,
        timestamp=timestamp,
        timestamp_inferred=inferred,
        sender="me",
        recipients=[],
        content=content,
        subject=subject_from(frontmatter, raw, md_file),
        thread_id=frontmatter.get("session_id") or None,
        lang=_chat_lang(frontmatter),
        raw_metadata={
          "path": str(rel),
          "session_id": frontmatter.get("session_id"),
          "cwd": frontmatter.get("cwd"),
          "git_branch": frontmatter.get("git_branch"),
          "model": frontmatter.get("model"),
          "tags": frontmatter.get("tags") or [],
          "mtime": mtime.isoformat(),
        },
      )


def _chat_timestamp(
  frontmatter: dict,
  path: Path,
  mtime: datetime,
) -> tuple[datetime, bool]:
  """Resolve a chat's timestamp. Priority: frontmatter date → filename date
  prefix → file mtime (the only case flagged inferred)."""
  for candidate in (
    dated_frontmatter_value(frontmatter, _FM_DATE_KEYS),
    date_from_filename(path),
  ):
    if candidate is not None:
      return candidate, False
  return mtime, True


def _chat_lang(frontmatter: dict) -> str | None:
  """Read the frontmatter lang, tolerating YAML's boolean coercion: an unquoted
  `lang: no` (Norwegian) parses as the boolean False, and `yes`/`on`/`true` as
  True. Map the False case back to 'no'; ignore truthy-bool noise."""
  lang = frontmatter.get("lang")
  if lang is False:
    return "no"
  if isinstance(lang, bool):
    return None
  return str(lang) if lang else None
