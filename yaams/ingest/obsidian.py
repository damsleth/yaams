from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc


_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_EMBED_RE = re.compile(r"!\[\[([^\]]*)\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_DATE_FROM_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")

DEFAULT_SKIP_DIRS = {".obsidian", ".git", ".smartchats", ".smart-env", ".claude"}
MIN_CONTENT_CHARS = 30


@dataclass
class ObsidianAdapter:
  vault_path: Path
  skip_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_SKIP_DIRS))
  skip_filename_prefixes: tuple[str, ...] = ("_",)
  skipped_empty: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_empty = 0
    vault = expand_path(self.vault_path)
    cutoff = ensure_utc(since)

    for md_file in _walk_vault(vault, self.skip_dirs, self.skip_filename_prefixes):
      mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue

      raw = md_file.read_text(encoding="utf-8", errors="replace")
      frontmatter = _parse_frontmatter(raw)
      content = _clean_content(raw)

      if len(content) < MIN_CONTENT_CHARS:
        self.skipped_empty += 1
        continue

      rel = md_file.relative_to(vault)
      source_id = str(rel)
      timestamp = _note_timestamp(frontmatter, md_file, mtime)
      subject = _note_subject(frontmatter, raw, md_file)
      thread_id = str(rel.parent) if str(rel.parent) != "." else ""

      yield Item(
        id=hash_id("notes", source_id),
        source="notes",
        source_id=source_id,
        timestamp=timestamp,
        sender="me",
        recipients=[],
        content=content,
        subject=subject,
        thread_id=thread_id or None,
        raw_metadata={
          "vault": str(vault),
          "path": str(rel),
          "tags": frontmatter.get("tags") or [],
          "mtime": mtime.isoformat(),
        },
      )


def _walk_vault(
  vault: Path,
  skip_dirs: set[str],
  skip_prefixes: tuple[str, ...],
) -> Iterator[Path]:
  for path in sorted(vault.rglob("*.md")):
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
  text = _EMBED_RE.sub("", text)
  text = _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
  text = re.sub(r"\n{3,}", "\n\n", text)
  return text.strip()


def _note_timestamp(
  frontmatter: dict,
  path: Path,
  mtime: datetime,
) -> datetime:
  # frontmatter date: field
  fm_date = frontmatter.get("date") or frontmatter.get("created")
  if fm_date:
    try:
      if isinstance(fm_date, datetime):
        return ensure_utc(fm_date)
      parsed = datetime.fromisoformat(str(fm_date))
      return ensure_utc(parsed)
    except (ValueError, TypeError):
      pass

  # YYYY-MM-DD prefix in filename
  m = _DATE_FROM_FILENAME.match(path.stem)
  if m:
    try:
      d = datetime.strptime(m.group(1), "%Y-%m-%d")
      return d.replace(tzinfo=UTC)
    except ValueError:
      pass

  return mtime


def _note_subject(frontmatter: dict, text: str, path: Path) -> str | None:
  title = frontmatter.get("title")
  if title:
    return str(title)
  m = _H1_RE.search(_FRONTMATTER_RE.sub("", text))
  if m:
    return m.group(1).strip()
  return path.stem
