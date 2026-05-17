"""Generic folder ingestion.

Walks one or more root paths recursively and yields one Item per supported
file. Plain text and markdown are read directly; PDF and DOCX are extracted
via optional dependencies (pypdf, python-docx). Images are ingested with
synthetic text content built from filename + folder hierarchy (plus EXIF
date/dimensions if Pillow is installed). The folder hierarchy is preserved
in raw_metadata.folder_path / folder_parts and exposed as thread_id so
location is queryable for every item.

Missing optional deps skip those file types and increment a counter rather
than erroring out.
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
DOCUMENT_EXTENSIONS = (".txt", ".md", ".markdown", ".pdf", ".docx")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tiff", ".tif")
DEFAULT_EXTENSIONS = DOCUMENT_EXTENSIONS + IMAGE_EXTENSIONS
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
  skipped_images: int = field(default=0, init=False)
  files_walked: int = field(default=0, init=False)
  skipped_before_cutoff: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_empty = 0
    self.skipped_unsupported = 0
    self.skipped_missing_dep = 0
    self.skipped_images = 0
    self.files_walked = 0
    self.skipped_before_cutoff = 0
    cutoff = ensure_utc(since)

    for raw_root in self.folder_paths:
      root = expand_path(raw_root)
      if not root.exists():
        continue
      for path in _walk_folder(root, self.skip_dirs, self.skip_filename_prefixes, self.extensions):
        self.files_walked += 1
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
          self.skipped_before_cutoff += 1
          continue

        rel = path.relative_to(root)
        folder_parts = list(rel.parent.parts) if str(rel.parent) != "." else []
        ext = path.suffix.lower()
        is_image = ext in IMAGE_EXTENSIONS

        content, subject, frontmatter, extra_meta = _extract(path, self, folder_parts)
        if content is None:
          continue
        if not is_image and len(content) < MIN_CONTENT_CHARS:
          self.skipped_empty += 1
          continue

        source_id = str(path)
        timestamp = _file_timestamp(frontmatter, path, mtime, extra_meta)
        thread_id = "/".join(folder_parts) if folder_parts else None

        raw_metadata = {
          "root": str(root),
          "path": str(rel),
          "folder_path": "/".join(folder_parts),
          "folder_parts": folder_parts,
          "filename": path.name,
          "ext": ext,
          "mtime": mtime.isoformat(),
          "kind": "image" if is_image else "document",
        }
        raw_metadata.update(extra_meta)

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
          raw_metadata=raw_metadata,
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


def _extract(
  path: Path, adapter: FolderAdapter, folder_parts: list[str]
) -> tuple[str | None, str | None, dict, dict]:
  """Return (content, subject, frontmatter, extra_metadata) or (None, ...) on skip."""
  ext = path.suffix.lower()
  if ext in (".txt",):
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or None, path.stem, {}, {}
  if ext in (".md", ".markdown"):
    raw = path.read_text(encoding="utf-8", errors="replace")
    frontmatter = _parse_frontmatter(raw)
    body = _FRONTMATTER_RE.sub("", raw).strip()
    subject = _md_subject(frontmatter, body, path)
    return body, subject, frontmatter, {}
  if ext == ".pdf":
    text = _read_pdf(path)
    if text is None:
      adapter.skipped_missing_dep += 1
      return None, None, {}, {}
    return text, path.stem, {}, {}
  if ext == ".docx":
    text = _read_docx(path)
    if text is None:
      adapter.skipped_missing_dep += 1
      return None, None, {}, {}
    return text, path.stem, {}, {}
  if ext in IMAGE_EXTENSIONS:
    exif = _read_image_exif(path)
    content = _image_content(path, folder_parts, exif)
    subject = _image_subject(path, folder_parts)
    return content, subject, {}, exif
  adapter.skipped_unsupported += 1
  return None, None, {}, {}


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


def _file_timestamp(
  frontmatter: dict, path: Path, mtime: datetime, extra_meta: dict | None = None
) -> datetime:
  fm_date = frontmatter.get("date") or frontmatter.get("created")
  if fm_date:
    try:
      if isinstance(fm_date, datetime):
        return ensure_utc(fm_date)
      parsed = datetime.fromisoformat(str(fm_date))
      return ensure_utc(parsed)
    except (ValueError, TypeError):
      pass
  if extra_meta:
    exif_dt = extra_meta.get("exif_datetime")
    if exif_dt:
      try:
        parsed = datetime.strptime(exif_dt, "%Y:%m:%d %H:%M:%S")
        return parsed.replace(tzinfo=UTC)
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


def _image_subject(path: Path, folder_parts: list[str]) -> str:
  if folder_parts:
    return f"{'/'.join(folder_parts)}/{path.stem}"
  return path.stem


def _image_content(path: Path, folder_parts: list[str], exif: dict) -> str:
  lines: list[str] = []
  lines.append(f"Image: {path.name}")
  if folder_parts:
    lines.append(f"Folder: {'/'.join(folder_parts)}")
    lines.append("Hierarchy: " + " > ".join(folder_parts))
  for key in ("exif_datetime", "image_width", "image_height", "camera_make", "camera_model"):
    val = exif.get(key)
    if val:
      lines.append(f"{key}: {val}")
  return "\n".join(lines)


def _read_image_exif(path: Path) -> dict:
  try:
    from PIL import Image, ExifTags
  except ImportError:
    return {}
  try:
    with Image.open(path) as img:
      width, height = img.size
      result: dict = {"image_width": width, "image_height": height}
      raw = getattr(img, "_getexif", lambda: None)()
      if raw:
        tags = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}
        dt = tags.get("DateTimeOriginal") or tags.get("DateTime")
        if dt:
          result["exif_datetime"] = str(dt).strip()
        make = tags.get("Make")
        if make:
          result["camera_make"] = str(make).strip()
        model = tags.get("Model")
        if model:
          result["camera_model"] = str(model).strip()
      return result
  except Exception:
    return {}
