"""Cloud-drive ingestion (Google Drive + OneDrive/M365).

A `drive` source syncs a profile's document files down to a local directory,
then reuses :class:`FolderAdapter` to index them — so all the pdf/docx/md/txt
extraction lives in one place. Files are relabelled `drive_<profile>` so they
sort separately from a plain `folders` source.

Provider is detected from the owa-piggy token shape: Google mints opaque
`ya29.` access tokens, Microsoft mints JWTs. Google-native Docs are exported to
markdown; everything else is downloaded only when its extension is one
FolderAdapter can read. Sync is incremental: a file is re-downloaded only when
the remote `modifiedTime` is newer than the local copy.

ponytail: only Google-native *Docs* are exported (Sheets/Slides/Forms skipped);
add CSV/PDF export paths if those turn out to matter.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest.base import Item, hash_id
from yaams.ingest.folder import DOCUMENT_EXTENSIONS, FolderAdapter
from yaams.time import ensure_utc, parse_iso_datetime

logger = logging.getLogger("yaams.ingest.drive")

GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
_UNSAFE_NAME = re.compile(r'[/\\:<>"|?*\x00-\x1f]+')


def _mint_token(profile: str) -> tuple[str, str]:
  """Return (token, provider) for a profile. provider is 'google' or 'm365'."""
  result = subprocess.run(
    ["owa-piggy", "--profile", profile],
    capture_output=True, text=True, check=True,
  )
  token = result.stdout.strip()
  if not token:
    raise RuntimeError(f"owa-piggy returned empty token for profile {profile}")
  # Google access tokens are opaque `ya29.` strings; Microsoft mints 3-part JWTs.
  provider = "google" if token.startswith("ya29.") else "m365"
  return token, provider


def _safe_filename(name: str) -> str:
  cleaned = _UNSAFE_NAME.sub("_", name).strip(". ")
  return cleaned or "untitled"


@dataclass
class DriveAdapter:
  profile: str
  local_dir: Path
  timeout: float = 60.0
  synced: int = field(default=0, init=False)
  skipped_unsupported: int = field(default=0, init=False)
  skipped_errors: int = field(default=0, init=False)
  # FolderAdapter counters proxied for the ingest stats table.
  files_walked: int = field(default=0, init=False)
  skipped_before_cutoff: int = field(default=0, init=False)
  skipped_empty: int = field(default=0, init=False)
  skipped_missing_dep: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.synced = 0
    self.skipped_unsupported = 0
    self.skipped_errors = 0
    dest = expand_path(self.local_dir)
    dest.mkdir(parents=True, exist_ok=True)
    self._sync(dest)

    folder = FolderAdapter(folder_paths=[dest], extensions=DOCUMENT_EXTENSIONS)
    source = f"drive_{self.profile}"
    # A document store isn't a time-series: "all documents" means every synced
    # file, regardless of the ingest `since` cutoff (docs predate it and would
    # otherwise be downloaded but never indexed, then frozen out by the
    # watermark). store_items still dedupes by id, so re-walks are cheap.
    floor = datetime.min.replace(tzinfo=UTC)
    for item in folder.extract(floor):
      yield Item(
        id=hash_id(source, item.source_id),
        source=source,
        source_id=item.source_id,
        timestamp=item.timestamp,
        sender=item.sender,
        recipients=item.recipients,
        content=item.content,
        subject=item.subject,
        thread_id=item.thread_id,
        raw_metadata={**item.raw_metadata, "profile": self.profile, "drive": True},
      )
    self.files_walked = folder.files_walked
    self.skipped_before_cutoff = folder.skipped_before_cutoff
    self.skipped_empty = folder.skipped_empty
    self.skipped_missing_dep = folder.skipped_missing_dep
    if folder.skipped_missing_dep:
      logger.warning(
        "drive %s: skipped %d PDF/DOCX file(s) — install extractors with "
        "`pip install 'yaams[drive]'` (or pypdf + python-docx) to index them",
        self.profile, folder.skipped_missing_dep,
      )

  # -- sync -----------------------------------------------------------------

  def _sync(self, dest: Path) -> None:
    import httpx

    token, provider = _mint_token(self.profile)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=self.timeout, headers=headers) as client:
      if provider == "google":
        self._sync_google(client, dest)
      else:
        self._sync_m365(client, dest)
    logger.info(
      "drive %s: synced %d file(s), skipped %d unsupported, %d error(s)",
      self.profile, self.synced, self.skipped_unsupported, self.skipped_errors,
    )

  def _try_download(self, client, url, params, target, modified) -> None:
    """Download one file; a per-file failure is skipped, never fatal."""
    import httpx
    try:
      self._download(client, url, params, target, modified)
    except httpx.HTTPStatusError as exc:
      # 403 on media = view-only / abuse-flagged; retry once acknowledging abuse.
      if exc.response.status_code == 403 and params and params.get("alt") == "media":
        try:
          self._download(client, url, {**params, "acknowledgeAbuse": "true"}, target, modified)
          return
        except httpx.HTTPStatusError:
          pass
      self.skipped_errors += 1
      logger.debug("drive %s: skipping %s: %s", self.profile, target.name, exc)
    except Exception as exc:
      self.skipped_errors += 1
      logger.debug("drive %s: skipping %s: %s", self.profile, target.name, exc)

  def _download(
    self, client, url: str, params: dict | None, target: Path, modified: datetime,
  ) -> None:
    """Write `url` to `target` unless the local copy is already current."""
    modified = ensure_utc(modified)
    if target.exists():
      local_mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)
      if local_mtime >= modified:
        return
    resp = client.get(url, params=params)
    resp.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(resp.content)
    ts = modified.timestamp()
    os.utime(target, (ts, ts))
    self.synced += 1

  def _sync_google(self, client, dest: Path) -> None:
    params = {
      "pageSize": "1000",
      "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
      "q": f"trashed=false and mimeType!='{GOOGLE_FOLDER_MIME}'",
    }
    page_token: str | None = None
    seen: dict[str, int] = {}
    while True:
      if page_token:
        params["pageToken"] = page_token
      data = client.get(f"{GOOGLE_DRIVE_API}/files", params=params).json()
      for f in data.get("files", []):
        self._sync_google_file(client, dest, f, seen)
      page_token = data.get("nextPageToken")
      if not page_token:
        break

  def _sync_google_file(self, client, dest: Path, f: dict, seen: dict[str, int]) -> None:
    mime = f.get("mimeType", "")
    name = _safe_filename(f.get("name", "untitled"))
    modified = parse_iso_datetime(f["modifiedTime"])
    file_id = f["id"]

    if mime == GOOGLE_DOC_MIME:
      stem = name
      url = f"{GOOGLE_DRIVE_API}/files/{file_id}/export"
      params: dict | None = {"mimeType": "text/markdown"}
      ext = ".md"
    else:
      ext = Path(name).suffix.lower()
      if ext not in DOCUMENT_EXTENSIONS:
        self.skipped_unsupported += 1
        return
      stem = Path(name).stem
      url = f"{GOOGLE_DRIVE_API}/files/{file_id}"
      params = {"alt": "media"}

    target = self._unique_target(dest, stem, ext, seen)
    self._try_download(client, url, params, target, modified)

  def _sync_m365(self, client, dest: Path) -> None:
    """Recursively walk a OneDrive tree, downloading document files.

    ponytail: verified only against Google (M365 auth was expired at build
    time); mirrors teams.py's Graph pagination — fix here if a live run trips.
    """
    seen: dict[str, int] = {}
    self._walk_m365(client, "/me/drive/root/children", dest, seen)

  def _walk_m365(self, client, url: str, dest: Path, seen: dict[str, int]) -> None:
    next_url: str | None = f"{GRAPH_BASE}{url}"
    while next_url:
      data = client.get(next_url).json()
      for item in data.get("value", []):
        if item.get("folder"):
          child_id = item["id"]
          self._walk_m365(client, f"/me/drive/items/{child_id}/children", dest, seen)
          continue
        name = _safe_filename(item.get("name", "untitled"))
        ext = Path(name).suffix.lower()
        if ext not in DOCUMENT_EXTENSIONS:
          self.skipped_unsupported += 1
          continue
        download_url = item.get("@microsoft.graph.downloadUrl")
        if not download_url:
          self.skipped_unsupported += 1
          continue
        modified = parse_iso_datetime(item["lastModifiedDateTime"])
        target = self._unique_target(dest, Path(name).stem, ext, seen)
        self._try_download(client, download_url, None, target, modified)
      next_url = data.get("@odata.nextLink")

  @staticmethod
  def _unique_target(dest: Path, stem: str, ext: str, seen: dict[str, int]) -> Path:
    """Stable path for a doc, disambiguating same-named files across runs."""
    key = f"{stem}{ext}".lower()
    n = seen.get(key, 0)
    seen[key] = n + 1
    if n:
      stem = f"{stem} ({n})"
    return dest / f"{stem}{ext}"
