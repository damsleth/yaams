"""Generic folder ingestion.

Walks one or more root paths recursively and yields one Item per supported
file. Plain text and markdown are read directly; PDF and DOCX are extracted
via optional dependencies (pypdf, python-docx). Missing dependencies skip
those file types and increment a counter rather than erroring out.
"""

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
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_DATE_FROM_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")

DEFAULT_SKIP_DIRS = {".git", ".obsidian", ".claude", "node_modules", "__pycache__", ".venv"}
DEFAULT_EXTENSIONS = (".txt", ".md", ".markdown", ".pdf", ".docx")
MIN_CONTENT_CHARS = 30


@dataclass
class FolderAdapter:
  folder_paths: list[Path]
  extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
  skip_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_SKIP_DIRS))
  skip_filename_prefixes: tuple[str, ...] = (".", "_")
  skipped_empty: int = field(default=0, init=False)
  skipped_unsupported: int = field(default=0, init=False)
  skipped_missing_dep: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_empty = 0
    self.skipped_unsupported = 0
    self.skipped_missing_dep = 0
    cutoff = ensure_utc(since)

    for raw_root in self.folder_paths:
      root = expand_path(raw_root)
      if not root.exists():
        continue
      for path in _walk_folder(root, self.skip_dirs, self.skip_filename_prefixes, self.extensions):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
          continue

        content, subject, frontmatter = _extract(path, self)
        if content is None:
          continue
        if len(content) < MIN_CONTENT_CHARS:
          self.skipped_empty += 1
          continue

        rel = path.relative_to(root)
        source_id = str(path)
        timestamp = _file_timestamp(frontmatter, path, mtime)
        thread_id = str(rel.parent) if str(rel.parent) != "." else None

        yield Item(
          id=hash_id("folders", source_id),
          source="folders",
          source_id=source_id,
          timestamp=timestamp,
          sender="me",
          recipients=[],
          content=content,
          subject=subject,
          thread_id=thread_id,
          raw_metadata={
            "root": str(root),
            "path": str(rel),
            "ext": path.suffix.lower(),
            "mtime": mtime.isoformat(),
          },
        )


def _walk_folder(
  root: Path,
  skip_dirs: set[str],
  skip_prefixes: tuple[str, ...],
  extensions: tuple[str, ...],
) -> Iterator[Path]:
  exts = {e.lower() for e in extensions}
  for path in sorted(root.rglob("*")):
    if not path.is_file():
      continue
    if any(part in skip_dirs for part in path.parts):
      continue
    if path.name.startswith(skip_prefixes):
      continue
    if path.suffix.lower() not in exts:
      continue
    yield path


def _extract(path: Path, adapter: FolderAdapter) -> tuple[str | None, str | None, dict]:
  """Return (content, subject, frontmatter) or (None, None, {}) on skip."""
  ext = path.suffix.lower()
  if ext in (".txt",):
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or None, path.stem, {}
  if ext in (".md", ".markdown"):
    raw = path.read_text(encoding="utf-8", errors="replace")
    frontmatter = _parse_frontmatter(raw)
    body = _FRONTMATTER_RE.sub("", raw).strip()
    subject = _md_subject(frontmatter, body, path)
    return body, subject, frontmatter
  if ext == ".pdf":
    text = _read_pdf(path)
    if text is None:
      adapter.skipped_missing_dep += 1
      return None, None, {}
    return text, path.stem, {}
  if ext == ".docx":
    text = _read_docx(path)
    if text is None:
      adapter.skipped_missing_dep += 1
      return None, None, {}
    return text, path.stem, {}
  adapter.skipped_unsupported += 1
  return None, None, {}


def _read_pdf(path: Path) -> str | None:
  try:
    from pypdf import PdfReader
  except ImportError:
    return None
  try:
    reader = PdfReader(str(path))
    chunks = []
    for page in reader.pages:
      try:
        chunks.append(page.extract_text() or "")
      except Exception:
        continue
    return "\n\n".join(c.strip() for c in chunks if c.strip()).strip()
  except Exception:
    return ""


def _read_docx(path: Path) -> str | None:
  try:
    import docx
  except ImportError:
    return None
  try:
    document = docx.Document(str(path))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs).strip()
  except Exception:
    return ""


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


def _md_subject(frontmatter: dict, body: str, path: Path) -> str:
  title = frontmatter.get("title")
  if title:
    return str(title)
  m = _H1_RE.search(body)
  if m:
    return m.group(1).strip()
  return path.stem


def _file_timestamp(frontmatter: dict, path: Path, mtime: datetime) -> datetime:
  fm_date = frontmatter.get("date") or frontmatter.get("created")
  if fm_date:
    try:
      if isinstance(fm_date, datetime):
        return ensure_utc(fm_date)
      parsed = datetime.fromisoformat(str(fm_date))
      return ensure_utc(parsed)
    except (ValueError, TypeError):
      pass
  m = _DATE_FROM_FILENAME.match(path.stem)
  if m:
    try:
      d = datetime.strptime(m.group(1), "%Y-%m-%d")
      return d.replace(tzinfo=UTC)
    except ValueError:
      pass
  return mtime
