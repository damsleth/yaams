from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from yaams.ingest.folder import FolderAdapter


def _touch(path: Path, body: str, mtime: datetime | None = None) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(body, encoding="utf-8")
  if mtime is not None:
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))
  return path


def test_folder_adapter_ingests_txt_and_md(tmp_path: Path) -> None:
  _touch(tmp_path / "alpha.txt", "Plain text body that is sufficiently long to pass the min-content threshold.")
  _touch(tmp_path / "notes" / "beta.md", "# Beta heading\n\nMarkdown body content with enough characters to pass.")
  _touch(tmp_path / "_skip_me.md", "# Skipped\n\nLeading underscore filenames are skipped.")
  _touch(tmp_path / ".hidden.txt", "Hidden file body that should be excluded from results.")
  _touch(tmp_path / ".git" / "config.txt", "Inside skip dir; ignored even though .txt.")

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  rel_paths = sorted(item.raw_metadata["path"] for item in items)
  assert rel_paths == ["alpha.txt", "notes/beta.md"]

  by_rel = {item.raw_metadata["path"]: item for item in items}
  assert by_rel["alpha.txt"].source == "folders"
  assert by_rel["alpha.txt"].sender == "me"
  assert by_rel["alpha.txt"].subject == "alpha"
  assert "Plain text body" in by_rel["alpha.txt"].content

  beta = by_rel["notes/beta.md"]
  assert beta.subject == "Beta heading"
  assert beta.thread_id == "notes"
  assert beta.raw_metadata["ext"] == ".md"


def test_folder_adapter_uses_frontmatter_date(tmp_path: Path) -> None:
  body = (
    "---\n"
    "title: Custom Title\n"
    "date: 2024-03-15\n"
    "---\n\n"
    "Markdown body content that is long enough to clear the minimum threshold."
  )
  _touch(tmp_path / "post.md", body)

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  assert len(items) == 1
  item = items[0]
  assert item.subject == "Custom Title"
  assert item.timestamp == datetime(2024, 3, 15, tzinfo=UTC)


def test_folder_adapter_filename_date_fallback(tmp_path: Path) -> None:
  body = "Plain text body that is long enough to be ingested without being filtered."
  _touch(tmp_path / "2026-05-12-meeting.txt", body)

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  assert len(items) == 1
  assert items[0].timestamp == datetime(2026, 5, 12, tzinfo=UTC)


def test_folder_adapter_skips_too_short(tmp_path: Path) -> None:
  _touch(tmp_path / "tiny.txt", "hi")

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  assert items == []
  assert adapter.skipped_empty == 1


def test_folder_adapter_respects_since(tmp_path: Path) -> None:
  _touch(
    tmp_path / "old.txt",
    "Old file body content with enough characters for the threshold.",
    mtime=datetime(2020, 1, 1, tzinfo=UTC),
  )
  _touch(
    tmp_path / "new.txt",
    "New file body content with enough characters for the threshold.",
    mtime=datetime(2026, 1, 1, tzinfo=UTC),
  )

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(2025, 1, 1, tzinfo=UTC)))

  assert {item.raw_metadata["path"] for item in items} == {"new.txt"}


def test_folder_adapter_filters_unsupported_extensions(tmp_path: Path) -> None:
  _touch(tmp_path / "keep.txt", "Plain text body long enough to pass the minimum threshold check easily.")
  _touch(tmp_path / "skip.html", "<html><body>Should never be ingested.</body></html>")

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  assert {item.raw_metadata["path"] for item in items} == {"keep.txt"}


def test_folder_adapter_walks_multiple_roots(tmp_path: Path) -> None:
  root_a = tmp_path / "a"
  root_b = tmp_path / "b"
  _touch(root_a / "alpha.txt", "Body for the file inside root A that is long enough to pass.")
  _touch(root_b / "beta.txt", "Body for the file inside root B that is long enough to pass.")

  adapter = FolderAdapter(folder_paths=[root_a, root_b])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  paths = {item.raw_metadata["path"] for item in items}
  roots = {item.raw_metadata["root"] for item in items}
  assert paths == {"alpha.txt", "beta.txt"}
  assert roots == {str(root_a), str(root_b)}


def test_folder_adapter_skips_missing_root(tmp_path: Path) -> None:
  real = tmp_path / "real"
  _touch(real / "x.txt", "Body content long enough to clear the minimum threshold filter.")
  adapter = FolderAdapter(folder_paths=[real, tmp_path / "does-not-exist"])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))
  assert len(items) == 1


def _touch_binary(path: Path, body: bytes = b"\x89PNG\r\n\x1a\n") -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(body)
  return path


def test_folder_adapter_ingests_images_with_hierarchy(tmp_path: Path) -> None:
  _touch_binary(tmp_path / "photos" / "2024" / "sandvika-byfest" / "img_001.jpg")
  _touch_binary(tmp_path / "photos" / "headshot.png")
  _touch(tmp_path / "notes.txt", "Plain text body long enough to clear the minimum threshold filter.")

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  by_path = {item.raw_metadata["path"]: item for item in items}
  assert set(by_path) == {
    "photos/2024/sandvika-byfest/img_001.jpg",
    "photos/headshot.png",
    "notes.txt",
  }

  nested = by_path["photos/2024/sandvika-byfest/img_001.jpg"]
  assert nested.raw_metadata["kind"] == "image"
  assert nested.raw_metadata["folder_path"] == "photos/2024/sandvika-byfest"
  assert nested.raw_metadata["folder_parts"] == ["photos", "2024", "sandvika-byfest"]
  assert nested.raw_metadata["filename"] == "img_001.jpg"
  assert nested.thread_id == "photos/2024/sandvika-byfest"
  assert nested.subject == "photos/2024/sandvika-byfest/img_001"
  assert "Image: img_001.jpg" in nested.content
  assert "Folder: photos/2024/sandvika-byfest" in nested.content
  assert "Hierarchy: photos > 2024 > sandvika-byfest" in nested.content

  shallow = by_path["photos/headshot.png"]
  assert shallow.raw_metadata["kind"] == "image"
  assert shallow.raw_metadata["folder_path"] == "photos"
  assert shallow.thread_id == "photos"

  doc = by_path["notes.txt"]
  assert doc.raw_metadata["kind"] == "document"
  assert doc.raw_metadata["folder_path"] == ""


def test_folder_adapter_image_short_content_not_filtered(tmp_path: Path) -> None:
  _touch_binary(tmp_path / "a.jpg")
  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  assert items[0].raw_metadata["kind"] == "image"
  assert adapter.skipped_empty == 0


def test_folder_adapter_files_walked_counts_all_matched_files(tmp_path: Path) -> None:
  _touch(tmp_path / "a.txt", "Body content long enough to clear the minimum threshold filter.")
  _touch(tmp_path / "nested" / "b.md", "# Beta\n\nBody content long enough to clear the threshold.")
  _touch_binary(tmp_path / "nested" / "deep" / "c.png")
  _touch(tmp_path / "skip.html", "<html>ignored ext</html>")

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(1970, 1, 1, tzinfo=UTC)))

  assert len(items) == 3
  assert adapter.files_walked == 3
  assert adapter.skipped_before_cutoff == 0


def test_folder_adapter_files_walked_includes_cutoff_filtered(tmp_path: Path) -> None:
  _touch(
    tmp_path / "old.txt",
    "Old file body content with enough characters for the threshold.",
    mtime=datetime(2020, 1, 1, tzinfo=UTC),
  )
  _touch(
    tmp_path / "new.txt",
    "New file body content with enough characters for the threshold.",
    mtime=datetime(2026, 1, 1, tzinfo=UTC),
  )

  adapter = FolderAdapter(folder_paths=[tmp_path])
  items = list(adapter.extract(datetime(2025, 1, 1, tzinfo=UTC)))

  assert len(items) == 1
  assert adapter.files_walked == 2
  assert adapter.skipped_before_cutoff == 1
