from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc

# Claude Code session summaries written by the capture-chat.sh SessionEnd hook.
# One markdown file per session, flat in the chats dir, with YAML frontmatter
# carrying created/updated/session_id/cwd/git_branch/model/tags/title/lang.

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_DATE_FROM_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# Frontmatter keys carrying a creation date, in priority order.
_FM_DATE_KEYS = ("created", "created_at", "date", "updated")

DEFAULT_SKIP_DIRS = {".git", ".obsidian", ".claude"}
MIN_CONTENT_CHARS = 30


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

    for md_file in _walk_chats(chats, self.skip_dirs, self.skip_filename_prefixes):
      mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue

      raw = md_file.read_text(encoding="utf-8", errors="replace")
      frontmatter = _parse_frontmatter(raw)
      content = _clean_content(raw)

      if len(content) < MIN_CONTENT_CHARS:
        self.skipped_empty += 1
        continue

      rel = md_file.relative_to(chats)
      source_id = str(rel)
      timestamp, inferred = _chat_timestamp(frontmatter, md_file, mtime)
      subject = _chat_subject(frontmatter, raw, md_file)
      lang = _chat_lang(frontmatter)

      yield Item(
        id=hash_id("chats", source_id),
        source="chats",
        source_id=source_id,
        timestamp=timestamp,
        timestamp_inferred=inferred,
        sender="me",
        recipients=[],
        content=content,
        subject=subject,
        thread_id=frontmatter.get("session_id") or None,
        lang=lang,
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


def _walk_chats(
  chats: Path,
  skip_dirs: set[str],
  skip_prefixes: tuple[str, ...],
) -> Iterator[Path]:
  for path in sorted(chats.rglob("*.md")):
    if any(part in skip_dirs for part in path.parts):
      continue
    if path.name.startswith(skip_prefixes):
      continue
    if path.name in {"README.md", "AGENTS.md"}:
      continue
    yield path


def _parse_frontmatter(text: str) -> dict:
  m = _FRONTMATTER_RE.match(text)
  if not m:
    return {}
  try:
    import yaml
    data = yaml.safe_load(m.group(0).strip("---\n")) or {}
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}


def _clean_content(text: str) -> str:
  text = _FRONTMATTER_RE.sub("", text)
  text = re.sub(r"\n{3,}", "\n\n", text)
  return text.strip()


def _chat_timestamp(
  frontmatter: dict,
  path: Path,
  mtime: datetime,
) -> tuple[datetime, bool]:
  """Resolve a chat's timestamp. Priority: frontmatter date → filename date
  prefix → file mtime (the only case flagged inferred)."""
  for key in _FM_DATE_KEYS:
    fm_date = frontmatter.get(key)
    if not fm_date:
      continue
    try:
      if isinstance(fm_date, datetime):
        return ensure_utc(fm_date), False
      parsed = datetime.fromisoformat(str(fm_date))
      return ensure_utc(parsed), False
    except (ValueError, TypeError):
      pass

  m = _DATE_FROM_FILENAME.match(path.stem)
  if m:
    try:
      d = datetime.strptime(m.group(1), "%Y-%m-%d")
      return d.replace(tzinfo=UTC), False
    except ValueError:
      pass

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


def _chat_subject(frontmatter: dict, text: str, path: Path) -> str | None:
  title = frontmatter.get("title")
  if title:
    return str(title)
  m = _H1_RE.search(_FRONTMATTER_RE.sub("", text))
  if m:
    return m.group(1).strip()
  return path.stem
