#!/usr/bin/env python3
"""Record one skill proposal to the autoresearch wiki (docs/experiments/wiki/).

The wiki is the persistent knowledge layer of the autoresearch loop, after
WikiSkill (arXiv 2608.27454). This is the one entry point for its audit
trail: every proposal against the skill surface (yaams/retrieve/*) is
preserved with its metadata, verdict, and full diff - rejected proposals
included - so later proposals can account for failed attempts instead of
repeating them. Each call writes one immutable file under wiki/proposals/
and appends one index line to wiki/evolution.md.

  CLI:     python docs/experiments/wiki.py --key my_idea --verdict discarded \
               --quality 0.6144 --delta 0.0 --round 3 --note "why it failed" \
               --diff-file /tmp/my_idea.diff
  import:  from wiki import record_proposal
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
WIKI = HERE / "wiki"
PROPOSALS_DIRNAME = "proposals"
EVOLUTION_NAME = "evolution.md"
VERDICTS = ("kept", "discarded", "crashed", "apply-failed", "parked")
_SEQ_RE = re.compile(r"(\d{4})-")


def _slug(key: str) -> str:
  s = re.sub(r"[^a-zA-Z0-9._-]+", "-", key.strip()).strip("-")
  return s or "proposal"


def _next_seq(proposals: Path) -> int:
  seqs = []
  for p in proposals.glob("*.md"):
    m = _SEQ_RE.match(p.name)
    if m:
      seqs.append(int(m.group(1)))
  return max(seqs, default=0) + 1


def record_proposal(key, verdict, *, quality=None, delta=None, round_no=None,
                    idea="", note="", diff="", commit="", date=None,
                    wiki_dir=None) -> Path:
  """Write one proposal file and append its evolution.md index line.

  Returns the path of the written proposal file. `wiki_dir` overrides the
  wiki root (tests); the proposals dir is created if missing.
  """
  if verdict not in VERDICTS:
    raise ValueError(f"bad verdict: {verdict!r}")
  wiki = Path(wiki_dir) if wiki_dir else WIKI
  proposals = wiki / PROPOSALS_DIRNAME
  proposals.mkdir(parents=True, exist_ok=True)
  date = date or dt.date.today().isoformat()
  seq = _next_seq(proposals)
  path = proposals / f"{seq:04d}-{_slug(key)}.md"

  lines = [f"# proposal {seq:04d}: {key}", "", f"- date: {date}",
           f"- verdict: {verdict}"]
  if quality is not None:
    lines.append(f"- quality: {quality}")
  if delta is not None:
    lines.append(f"- delta: {delta:+g}")
  if round_no is not None:
    lines.append(f"- round: {round_no}")
  if commit:
    lines.append(f"- commit: {commit}")
  if idea:
    lines.append(f"- idea: {idea}")
  if note:
    lines.append(f"- note: {note}")
  lines.append("")
  if diff.strip():
    lines += ["## diff", "", "```diff", diff.rstrip("\n"), "```", ""]
  else:
    lines += ["(no diff captured)", ""]
  path.write_text("\n".join(lines))

  d = f" delta={delta:+g}" if delta is not None else ""
  n = f" - {note}" if note else ""
  entry = (f"- {seq:04d} {date} [{key}]({PROPOSALS_DIRNAME}/{path.name})"
           f" {verdict}{d}{n}\n")
  with (wiki / EVOLUTION_NAME).open("a") as fh:
    fh.write(entry)
  return path


def _cli(argv=None) -> int:
  ap = argparse.ArgumentParser(description="Record a skill proposal to the wiki.")
  ap.add_argument("--key", required=True)
  ap.add_argument("--verdict", required=True, choices=list(VERDICTS))
  ap.add_argument("--quality", type=float)
  ap.add_argument("--delta", type=float)
  ap.add_argument("--round", type=int, dest="round_no")
  ap.add_argument("--idea", default="")
  ap.add_argument("--note", default="")
  ap.add_argument("--commit", default="")
  ap.add_argument("--date", default=None)
  ap.add_argument("--diff-file", dest="diff_file",
                  help="path to the proposal diff; '-' reads stdin")
  a = ap.parse_args(argv)
  diff = ""
  if a.diff_file:
    diff = sys.stdin.read() if a.diff_file == "-" else Path(a.diff_file).read_text()
  path = record_proposal(a.key, a.verdict, quality=a.quality, delta=a.delta,
                         round_no=a.round_no, idea=a.idea, note=a.note,
                         commit=a.commit, date=a.date, diff=diff)
  try:
    shown = path.relative_to(HERE)
  except ValueError:
    shown = path
  print(f"recorded {shown}")
  return 0


if __name__ == "__main__":
  raise SystemExit(_cli())
