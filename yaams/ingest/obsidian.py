from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest._markdown import (
  H1_RE,
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

_EMBED_RE = re.compile(r"!\[\[([^\]]*)\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

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

    for md_file in walk_markdown(vault, self.skip_dirs, self.skip_filename_prefixes):
      mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue

      raw = md_file.read_text(encoding="utf-8", errors="replace")
      frontmatter = parse_frontmatter(raw)
      content = _clean_content(raw)

      if len(content) < MIN_CONTENT_CHARS:
        self.skipped_empty += 1
        continue

      rel = md_file.relative_to(vault)
      source_id = str(rel)
      timestamp, inferred = _note_timestamp(frontmatter, md_file, content, mtime)
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
        subject=subject_from(frontmatter, raw, md_file),
        thread_id=thread_id or None,
        raw_metadata={
          "vault": str(vault),
          "path": str(rel),
          "tags": frontmatter.get("tags") or [],
          "mtime": mtime.isoformat(),
        },
      )


def _clean_content(text: str) -> str:
  """Frontmatter strip, then the Obsidian-only markup: drop embeds and unwrap
  wikilinks to their display text."""
  text = strip_frontmatter(text)
  text = _EMBED_RE.sub("", text)
  text = _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
  return collapse_blank_lines(text)


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
  for candidate in (
    dated_frontmatter_value(frontmatter, _FM_DATE_KEYS),
    date_from_filename(path),
    _date_from_title(frontmatter, content),
  ):
    if candidate is not None:
      return candidate, False
  return mtime, True


def _date_from_title(frontmatter: dict, content: str) -> datetime | None:
  """Parse a leading date out of the note title or first heading.

  Handles Norwegian/English daily-note headers ("18.mai 2026", "18.October
  2024") and a plain ISO date. Returns None when no date is present."""
  candidates: list[str] = []
  title = frontmatter.get("title")
  if title:
    candidates.append(str(title))
  h1 = H1_RE.search(content)
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
