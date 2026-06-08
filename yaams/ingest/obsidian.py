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

# Frontmatter keys that carry an authorship/creation date, in priority order.
_FM_DATE_KEYS = ("date", "created", "created_at", "date created")

# Daily-note headers like "# 18.mai 2026 - mandag" or "# 18.October 2024".
# Month names cover Norwegian (bokmål) and English, full + common 3-letter.
_MONTHS = {
  "januar": 1, "january": 1, "jan": 1,
  "februar": 2, "february": 2, "feb": 2,
  "mars": 3, "march": 3, "mar": 3,
  "april": 4, "apr": 4,
  "mai": 5, "may": 5,
  "juni": 6, "june": 6, "jun": 6,
  "juli": 7, "july": 7, "jul": 7,
  "august": 8, "aug": 8,
  "september": 9, "sep": 9, "sept": 9,
  "oktober": 10, "october": 10, "oct": 10, "okt": 10,
  "november": 11, "nov": 11,
  "desember": 12, "december": 12, "dec": 12, "des": 12,
}
_DAY_MONTH_YEAR_RE = re.compile(
  r"\b(\d{1,2})\.\s*([A-Za-zæøåÆØÅ]+)\.?\s+(\d{4})\b"
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

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
      timestamp, inferred = _note_timestamp(frontmatter, md_file, content, mtime)
      subject = _note_subject(frontmatter, raw, md_file)
      thread_id = str(rel.parent) if str(rel.parent) != "." else ""

      yield Item(
        id=hash_id("notes", source_id),
        source="notes",
        source_id=source_id,
        timestamp=timestamp,
        timestamp_inferred=inferred,
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
  content: str,
  mtime: datetime,
) -> tuple[datetime, bool]:
  """Resolve a note's timestamp and whether it was inferred from mtime.

  Priority: frontmatter date → filename date prefix → date in the title /
  first heading → mtime. Only the mtime fallback counts as *inferred*: a bulk
  vault import collapses every undated note onto one recent mtime, which makes
  them masquerade as the freshest items in the corpus. Flagging that lets
  recency-sorted retrieval keep undated notes out of "what's the latest".

  Returns ``(timestamp, inferred)``.
  """
  # 1. Frontmatter date field.
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

  # 2. YYYY-MM-DD prefix in filename.
  m = _DATE_FROM_FILENAME.match(path.stem)
  if m:
    try:
      d = datetime.strptime(m.group(1), "%Y-%m-%d")
      return d.replace(tzinfo=UTC), False
    except ValueError:
      pass

  # 3. A date in the title / first H1 (e.g. daily note "# 18.mai 2026").
  title_date = _date_from_title(frontmatter, content)
  if title_date is not None:
    return title_date, False

  # 4. No real signal — fall back to file mtime and mark it inferred.
  return mtime, True


def _date_from_title(frontmatter: dict, content: str) -> datetime | None:
  """Parse a leading date out of the note title or first heading.

  Handles Norwegian/English daily-note headers ("18.mai 2026", "18.October
  2024") and a plain ISO date. Returns None when no date is present."""
  candidates: list[str] = []
  title = frontmatter.get("title")
  if title:
    candidates.append(str(title))
  h1 = _H1_RE.search(content)
  if h1:
    candidates.append(h1.group(1))

  for text in candidates:
    dm = _DAY_MONTH_YEAR_RE.search(text)
    if dm:
      month = _MONTHS.get(dm.group(2).lower())
      if month:
        try:
          return datetime(int(dm.group(3)), month, int(dm.group(1)), tzinfo=UTC)
        except ValueError:
          pass
    iso = _ISO_DATE_RE.search(text)
    if iso:
      try:
        return datetime(
          int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=UTC
        )
      except ValueError:
        pass
  return None


def _note_subject(frontmatter: dict, text: str, path: Path) -> str | None:
  title = frontmatter.get("title")
  if title:
    return str(title)
  m = _H1_RE.search(_FRONTMATTER_RE.sub("", text))
  if m:
    return m.group(1).strip()
  return path.stem
