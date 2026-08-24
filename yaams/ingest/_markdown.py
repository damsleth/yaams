"""Shared markdown-file plumbing for the note-shaped ingest adapters.

`obsidian.py` and `chats.py` both walk a tree of markdown files with YAML
frontmatter and turn each one into an Item. Only the *policy* differs between
them — which frontmatter keys carry a date and in what order, which directories
to skip, whether wikilinks need stripping, where the thread id comes from. The
mechanics below are byte-identical in both, so they live here once.

Deliberately NOT shared, because the two adapters disagree on purpose:
  - frontmatter date-key priority (obsidian prefers `date`, chats `created`)
  - content cleaning beyond the frontmatter strip (obsidian also drops embeds
    and unwraps wikilinks)
  - the title/heading date rung (obsidian daily notes only)
  - skip dirs, skip prefixes, thread id, and raw_metadata
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Sequence

from yaams.time import ensure_utc

FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
DATE_FROM_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# Never ingested as content from any markdown tree.
SKIP_FILENAMES = {"README.md", "AGENTS.md"}

MIN_CONTENT_CHARS = 30


def walk_markdown(
  root: Path,
  skip_dirs: set[str],
  skip_prefixes: tuple[str, ...],
) -> Iterator[Path]:
  """Yield every ingestable .md file under `root`, sorted for stable ids."""
  for path in sorted(root.rglob("*.md")):
    if any(part in skip_dirs for part in path.parts):
      continue
    if path.name.startswith(skip_prefixes):
      continue
    if path.name in SKIP_FILENAMES:
      continue
    yield path


def parse_frontmatter(text: str) -> dict:
  """Parse the leading YAML frontmatter block. Empty dict when absent or bad."""
  m = FRONTMATTER_RE.match(text)
  if not m:
    return {}
  try:
    import yaml

    data = yaml.safe_load(m.group(0).strip("---\n")) or {}
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}


def strip_frontmatter(text: str) -> str:
  return FRONTMATTER_RE.sub("", text)


def collapse_blank_lines(text: str) -> str:
  return re.sub(r"\n{3,}", "\n\n", text).strip()


def subject_from(frontmatter: dict, text: str, path: Path) -> str | None:
  """Frontmatter title -> first H1 -> filename stem."""
  title = frontmatter.get("title")
  if title:
    return str(title)
  m = H1_RE.search(strip_frontmatter(text))
  if m:
    return m.group(1).strip()
  return path.stem


def dated_frontmatter_value(
  frontmatter: dict,
  date_keys: Sequence[str],
) -> datetime | None:
  """First parseable date among `date_keys`, in the order given.

  The order is the caller's policy — obsidian leads with `date`, chats with
  `created` — so it stays a parameter rather than a shared constant.
  """
  for key in date_keys:
    value = frontmatter.get(key)
    if not value:
      continue
    try:
      if isinstance(value, datetime):
        return ensure_utc(value)
      return ensure_utc(datetime.fromisoformat(str(value)))
    except (ValueError, TypeError):
      pass
  return None


def date_from_filename(path: Path) -> datetime | None:
  """A leading YYYY-MM-DD in the filename, as a UTC midnight datetime."""
  m = DATE_FROM_FILENAME.match(path.stem)
  if not m:
    return None
  try:
    return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
  except ValueError:
    return None
