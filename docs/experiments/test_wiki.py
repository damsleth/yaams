#!/usr/bin/env python3
"""Self-check for the wiki proposal recorder. Runs under pytest or directly.

Guards the audit-trail invariants: rejected diffs are preserved verbatim,
sequence numbers stay monotonic across the READMEs living in the same dir,
and evolution.md is append-only per call.
Run: python docs/experiments/test_wiki.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import wiki


def _fresh(tmp: str) -> Path:
  w = Path(tmp) / "wiki"
  (w / wiki.PROPOSALS_DIRNAME).mkdir(parents=True)
  (w / wiki.PROPOSALS_DIRNAME / "README.md").write_text("# readme\n")
  (w / wiki.EVOLUTION_NAME).write_text("# log\n")
  return w


def test_sequence_ignores_readme_and_increments():
  with tempfile.TemporaryDirectory() as tmp:
    w = _fresh(tmp)
    p1 = wiki.record_proposal("idea one", "discarded", wiki_dir=w, date="2026-08-30")
    p2 = wiki.record_proposal("idea-two", "kept", delta=0.03, wiki_dir=w, date="2026-08-30")
    assert p1.name == "0001-idea-one.md"
    assert p2.name == "0002-idea-two.md"


def test_rejected_diff_preserved_verbatim():
  diff = "--- a/yaams/retrieve/hybrid.py\n+++ b/yaams/retrieve/hybrid.py\n@@ -1 +1 @@\n-old\n+new"
  with tempfile.TemporaryDirectory() as tmp:
    w = _fresh(tmp)
    p = wiki.record_proposal("dead_idea", "discarded", quality=0.61, delta=-0.01,
                             round_no=2, note="regressed", diff=diff,
                             wiki_dir=w, date="2026-08-30")
    body = p.read_text()
    assert diff in body
    assert "- verdict: discarded" in body
    assert "- delta: -0.01" in body
    assert "- round: 2" in body


def test_evolution_appends_one_line_per_proposal():
  with tempfile.TemporaryDirectory() as tmp:
    w = _fresh(tmp)
    wiki.record_proposal("a", "crashed", wiki_dir=w, date="2026-08-30")
    wiki.record_proposal("b", "kept", delta=0.02, note="win", wiki_dir=w, date="2026-08-30")
    lines = (w / wiki.EVOLUTION_NAME).read_text().splitlines()
    assert lines[0] == "# log"
    assert lines[1].startswith("- 0001 2026-08-30 [a](proposals/0001-a.md) crashed")
    assert lines[2] == "- 0002 2026-08-30 [b](proposals/0002-b.md) kept delta=+0.02 - win"


def test_no_diff_is_marked():
  with tempfile.TemporaryDirectory() as tmp:
    w = _fresh(tmp)
    p = wiki.record_proposal("nochange", "crashed", wiki_dir=w, date="2026-08-30")
    assert "(no diff captured)" in p.read_text()


def test_bad_verdict_rejected():
  with tempfile.TemporaryDirectory() as tmp:
    w = _fresh(tmp)
    try:
      wiki.record_proposal("x", "won", wiki_dir=w)
    except ValueError:
      pass
    else:
      raise AssertionError("verdict 'won' should raise")


def test_cli_roundtrip():
  with tempfile.TemporaryDirectory() as tmp:
    w = _fresh(tmp)
    df = Path(tmp) / "x.diff"
    df.write_text("+one line\n")
    orig = wiki.WIKI
    try:
      wiki.WIKI = w
      rc = wiki._cli(["--key", "cli_idea", "--verdict", "discarded",
                      "--quality", "0.62", "--delta", "0.0", "--round", "1",
                      "--note", "neutral", "--date", "2026-08-30",
                      "--diff-file", str(df)])
    finally:
      wiki.WIKI = orig
    assert rc == 0
    assert "+one line" in (w / wiki.PROPOSALS_DIRNAME / "0001-cli_idea.md").read_text()


if __name__ == "__main__":
  for name, fn in sorted(globals().items()):
    if name.startswith("test_"):
      fn()
      print(f"ok  {name}")
  print("\nwiki recorder checked.")
