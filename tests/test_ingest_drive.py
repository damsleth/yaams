from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from yaams.ingest.drive import DriveAdapter, _mint_token, _safe_filename


def test_safe_filename():
  assert _safe_filename("a/b:c*?.md") == "a_b_c_.md"
  assert _safe_filename("  ...  ") == "untitled"
  assert _safe_filename("normal name.pdf") == "normal name.pdf"


def test_unique_target_dedupes_same_name():
  seen: dict[str, int] = {}
  a = DriveAdapter._unique_target(Path("/d"), "report", ".md", seen)
  b = DriveAdapter._unique_target(Path("/d"), "report", ".md", seen)
  assert a.name == "report.md"
  assert b.name == "report (1).md"  # collision disambiguated


def test_provider_detection(monkeypatch):
  import subprocess

  def fake_run(cmd, **kw):
    class R:
      stdout = "ya29.OPAQUE-GOOGLE-TOKEN\n"
    return R()

  monkeypatch.setattr(subprocess, "run", fake_run)
  _tok, provider = _mint_token("brkh-g")
  assert provider == "google"

  def fake_jwt(cmd, **kw):
    class R:
      stdout = "eyJhbGc.eyJzdWIi.sig\n"
    return R()

  monkeypatch.setattr(subprocess, "run", fake_jwt)
  _tok, provider = _mint_token("work")
  assert provider == "m365"


def test_extract_relabels_synced_files(tmp_path, monkeypatch):
  """A file dropped by _sync is indexed and relabelled drive_<profile>."""
  def fake_sync(self, dest: Path) -> None:
    doc = dest / "note.md"
    doc.write_text("# Title\n\nEnough body text to clear the min length gate.\n")

  monkeypatch.setattr(DriveAdapter, "_sync", fake_sync)
  adapter = DriveAdapter(profile="brkh-g", local_dir=tmp_path)
  items = list(adapter.extract(datetime(2015, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  assert items[0].source == "drive_brkh-g"
  assert items[0].raw_metadata["profile"] == "brkh-g"
  assert items[0].raw_metadata["drive"] is True
