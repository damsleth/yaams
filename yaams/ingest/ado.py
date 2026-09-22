"""Azure DevOps ingestion (work items + wiki pages) via the ``owa-ado`` CLI.

Shells out to ``owa-ado`` the same way calendar/mail/drive shell out to their
owa-* siblings: yaams passes ``--profile <p>`` and owa-ado's own pinned
org/project config plus owa-piggy auth do the rest. Never imports owa_ado
internals. One yaams source per profile: ``ado_<profile>``.

Work items are mutable, so the revision is encoded in ``source_id``
(``"{id}:{rev}"``): an edit hashes to a new item instead of rewriting history.
Wiki pages carry no usable server-side timestamp, so the mirrored file's mtime
is used with ``timestamp_inferred=True`` and the body hash goes into
``source_id`` (``"{path}:{sha8}"``) for change detection.

Shape verified live 2026-09-22 (profile ``nc``):
  - ``wi --query <wiql> --top N`` returns compact rows
    ``{id, type, title, state, assignedTo, iteration, area, tags, changed, url}``
    capped at ``--top`` (default 50, no ``--all``), so paging is keyed on
    ``[System.Id] > last``.
  - WIQL date literals: ``'YYYY-MM-DD'`` and ``'...T00:00:00Z'`` work; any
    non-midnight time component is an HTTP 400 (the REST call lacks
    ``timePrecision``), hence the date-only literal below.
  - ``wi <id> --full`` is the raw REST payload: ``{id, rev, fields{...},
    multilineFieldsFormat, relations}`` with HTML in Description /
    AcceptanceCriteria / ReproSteps. There is no comment *read* verb, so
    comments are not inlined; ``System.CommentCount`` is kept in metadata.
  - ``wiki --download <dir>`` mirrors pages to ``<dir>/<gitItemPath>`` as
    markdown and prints ``{downloaded, dir, files}``; a project with no wiki
    exits non-zero.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

from yaams.config import expand_path
from yaams.ingest._markdown import MIN_CONTENT_CHARS, collapse_blank_lines, walk_markdown
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc, parse_iso_datetime

logger = logging.getLogger("yaams.ingest.ado")

CONTENT_TYPES = ("workitems", "wiki")
HTML_FIELDS = (
  ("System.Description", "Description"),
  ("Microsoft.VSTS.Common.AcceptanceCriteria", "Acceptance criteria"),
  ("Microsoft.VSTS.TCM.ReproSteps", "Repro steps"),
  ("Microsoft.VSTS.TCM.SystemInfo", "System info"),
)
_BLOCK_TAGS = {
  "p", "div", "br", "li", "ul", "ol", "tr", "table", "blockquote", "pre",
  "h1", "h2", "h3", "h4", "h5", "h6",
}
_DROP_TAGS = {"style", "script", "head"}


class _TextExtractor(HTMLParser):
  def __init__(self) -> None:
    super().__init__()
    self.parts: list[str] = []
    self._drop = 0

  def handle_starttag(self, tag: str, attrs) -> None:
    if tag in _DROP_TAGS:
      self._drop += 1
    elif tag in _BLOCK_TAGS:
      self.parts.append("\n")

  def handle_endtag(self, tag: str) -> None:
    if tag in _DROP_TAGS:
      self._drop = max(0, self._drop - 1)
    elif tag in _BLOCK_TAGS:
      self.parts.append("\n")

  def handle_data(self, data: str) -> None:
    if not self._drop:
      self.parts.append(data)


def strip_html(text: str) -> str:
  """ADO rich-text fields to plain text, block tags becoming line breaks."""
  if "<" not in text:
    return collapse_blank_lines(html.unescape(text))
  parser = _TextExtractor()
  parser.feed(text)
  parser.close()
  lines = (line.strip() for line in "".join(parser.parts).splitlines())
  return "\n".join(line for line in lines if line)


def _identity(value) -> str:
  if isinstance(value, dict):
    return (value.get("displayName") or value.get("uniqueName") or "").strip()
  return str(value or "").strip()


def wiki_page_title(path: Path) -> str:
  """Mirror filenames encode spaces as ``-`` and literal dashes as ``%2D``."""
  return unquote(path.stem.replace("-", " ").replace("%2D", "-"))


@dataclass
class AdoAdapter:
  profile: str
  wiki_dir: Path
  content_types: tuple[str, ...] = CONTENT_TYPES
  project: str | None = None
  top: int = 500
  max_workers: int = 4
  timeout: float = 60.0
  # A full-wiki mirror is one long owa-ado call (156 pages took >60s live).
  wiki_timeout: float = 900.0
  work_items: int = field(default=0, init=False)
  wiki_pages: int = field(default=0, init=False)
  skipped_empty: int = field(default=0, init=False)
  skipped_errors: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.work_items = 0
    self.wiki_pages = 0
    self.skipped_empty = 0
    self.skipped_errors = 0
    cutoff = ensure_utc(since)
    if "workitems" in self.content_types:
      yield from self._extract_work_items(cutoff)
    if "wiki" in self.content_types:
      yield from self._extract_wiki()

  # -- work items -------------------------------------------------------------

  def _extract_work_items(self, cutoff: datetime) -> Iterator[Item]:
    ids = self._changed_ids(cutoff)
    with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
      payloads = list(pool.map(self._fetch_full, ids))
    for payload in payloads:
      if not payload:
        self.skipped_errors += 1
        continue
      item = work_item_to_item(payload, self.profile)
      if item is None:
        self.skipped_empty += 1
        continue
      self.work_items += 1
      yield item

  def _changed_ids(self, cutoff: datetime) -> list[int]:
    """Page WIQL on ``[System.Id] > last`` until a page comes back short.

    The date literal is date-only: a time component is an HTTP 400 without
    ``timePrecision``, and the server reads the literal in its own zone, so
    the query starts a day early. The over-fetch is absorbed by the
    ``changed`` filter here and by ``hash_id`` dedup downstream.
    """
    day = (cutoff - timedelta(days=1)).date().isoformat()
    ids: list[int] = []
    last = 0
    while True:
      wiql = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.ChangedDate] >= '{day}' AND [System.Id] > {last} "
        "ORDER BY [System.Id] ASC"
      )
      rows = self._run(["wi", "--query", wiql, "--top", str(self.top)])
      if not isinstance(rows, list) or not rows:
        break
      page_ids: list[int] = []
      for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
          continue
        page_ids.append(int(row["id"]))
        changed = row.get("changed")
        try:
          if changed and parse_iso_datetime(changed) < cutoff:
            continue
        except ValueError:
          pass
        ids.append(int(row["id"]))
      if not page_ids or len(rows) < self.top:
        break
      last = max(page_ids)
    return ids

  def _fetch_full(self, work_item_id: int) -> dict | None:
    data = self._run(["wi", str(work_item_id), "--full"])
    return data if isinstance(data, dict) else None

  # -- wiki -------------------------------------------------------------------

  def _extract_wiki(self) -> Iterator[Item]:
    dest = expand_path(self.wiki_dir) / self.profile
    dest.mkdir(parents=True, exist_ok=True)
    result = self._run(["wiki", "--download", str(dest)], timeout=self.wiki_timeout)
    if result is None:
      logger.info("ado %s: no wiki mirrored (project has none or download failed)", self.profile)
    for path in walk_markdown(dest, skip_dirs={".attachments"}, skip_prefixes=()):
      item = wiki_page_to_item(path, dest, self.profile)
      if item is None:
        self.skipped_empty += 1
        continue
      self.wiki_pages += 1
      yield item

  # -- subprocess -------------------------------------------------------------

  def _run(self, args: list[str], timeout: float | None = None) -> object | None:
    """``owa-ado <args> --profile <p> [--project <x>]``; JSON stdout or None."""
    cmd = ["owa-ado", *args, "--profile", self.profile]
    if self.project:
      cmd += ["--project", self.project]
    try:
      result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout or self.timeout,
      )
    except (subprocess.TimeoutExpired, OSError) as exc:
      logger.warning("owa-ado %s failed (profile=%s): %s", args[0], self.profile, exc)
      return None
    if result.returncode != 0:
      logger.warning(
        "owa-ado %s failed (profile=%s rc=%d): %s",
        args[0], self.profile, result.returncode,
        (result.stderr or "").strip() or "no stderr",
      )
      return None
    if not result.stdout.strip():
      return None
    try:
      return json.loads(result.stdout)
    except json.JSONDecodeError:
      logger.warning("owa-ado %s returned non-JSON (profile=%s)", args[0], self.profile)
      return None


def work_item_to_item(payload: dict, profile: str) -> Item | None:
  """One ``wi <id> --full`` payload to an Item; revision lives in source_id."""
  fields = payload.get("fields") or {}
  work_item_id = payload.get("id") or fields.get("System.Id")
  rev = payload.get("rev") or fields.get("System.Rev")
  title = (fields.get("System.Title") or "").strip()
  changed = fields.get("System.ChangedDate")
  if work_item_id is None or rev is None or not title or not changed:
    return None
  try:
    timestamp = parse_iso_datetime(changed)
  except ValueError:
    return None

  sections = [title]
  for key, label in HTML_FIELDS:
    body = strip_html(fields.get(key) or "")
    if body:
      sections.append(f"{label}:\n{body}")
  content = "\n\n".join(sections)

  parent = fields.get("System.Parent")
  tags = [t.strip() for t in (fields.get("System.Tags") or "").split(";") if t.strip()]
  assigned = _identity(fields.get("System.AssignedTo"))
  source = f"ado_{profile}"
  source_id = f"{work_item_id}:{rev}"
  return Item(
    id=hash_id(source, source_id),
    source=source,
    source_id=source_id,
    timestamp=timestamp,
    sender=_identity(fields.get("System.CreatedBy")) or "unknown",
    recipients=[assigned] if assigned else [],
    content=content,
    subject=title,
    thread_id=str(parent) if parent is not None else fields.get("System.AreaPath"),
    raw_metadata={
      "profile": profile,
      "kind": "workitem",
      "work_item_id": work_item_id,
      "rev": rev,
      "type": fields.get("System.WorkItemType"),
      "state": fields.get("System.State"),
      "tags": tags,
      "iteration": fields.get("System.IterationPath"),
      "area": fields.get("System.AreaPath"),
      "project": fields.get("System.TeamProject"),
      "parent": parent,
      "assigned_to": assigned or None,
      "changed_by": _identity(fields.get("System.ChangedBy")) or None,
      "created": fields.get("System.CreatedDate"),
      "comment_count": fields.get("System.CommentCount", 0),
      "url": ((payload.get("_links") or {}).get("html") or {}).get("href") or payload.get("url"),
    },
  )


def wiki_page_to_item(path: Path, root: Path, profile: str) -> Item | None:
  """One mirrored wiki page to an Item; body hash in source_id, mtime inferred."""
  try:
    raw = path.read_text(encoding="utf-8", errors="replace")
  except OSError:
    return None
  body = strip_html(raw)
  if len(body) < MIN_CONTENT_CHARS:
    return None
  rel = path.relative_to(root)
  page_path = "/" + rel.with_suffix("").as_posix()
  digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
  title = wiki_page_title(path)
  source = f"ado_{profile}"
  source_id = f"{page_path}:{digest}"
  parent = rel.parent.as_posix()
  return Item(
    id=hash_id(source, source_id),
    source=source,
    source_id=source_id,
    timestamp=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
    sender="wiki",
    recipients=[],
    content=f"{title}\n\n{body}",
    subject=title,
    thread_id=f"/{parent}" if parent != "." else "/",
    raw_metadata={
      "profile": profile,
      "kind": "wiki",
      "page_path": page_path,
      "file": str(path),
    },
    timestamp_inferred=True,
  )
