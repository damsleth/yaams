#!/usr/bin/env python3
"""Abbreviation discovery over the frozen promotion fixture (agentisk plan, PR 2).

Two read-only stages, no ledger writes, no entity mutation:

  mine   Scan non-holdout fixture items for explicit abbreviation patterns
         (`Lang form (KORT)`, `KORT (lang form)`, `KORT = lang`, «står for»,
         «forkortes», «kalles», "stands for", aka) and write normalized
         candidate relations to ~/brain/promotion_abbrev_mined.jsonl.
         Holdout items are never read: their evidence is reserved for the
         final eval (promotion_splits.py defines the splits).

  label  Join the unlabeled worksheet against mined evidence plus cheap
         shape heuristics (phone/email -> identifier, initials -> initialism,
         contraction -> abbreviation, person fallback -> nickname) and write
         a pre-labeled gold-set draft to ~/brain/promotion_abbrev_gold.csv.
         Every heuristic label carries label_source + needs_review so the
         human pass reviews instead of starting from nothing.

  gold-manifest  After review, pin the gold set: row/label counts and the
         CSV's sha256 into scripts/abbrev_gold_manifest.json (non-sensitive).

Usage:
    .venv/bin/python scripts/abbrev_mine.py mine
    .venv/bin/python scripts/abbrev_mine.py label
    .venv/bin/python scripts/abbrev_mine.py gold-manifest
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from promotion_splits import split_of  # noqa: E402

from yaams.db import open_db  # noqa: E402

FIXTURE = Path.home() / "brain" / "promotion_fixture.db"
WORKSHEET = Path.home() / "brain" / "promotion_abbrev_worksheet.csv"
MINED = Path.home() / "brain" / "promotion_abbrev_mined.jsonl"
GOLD = Path.home() / "brain" / "promotion_abbrev_gold.csv"
GOLD_MANIFEST = _REPO / "scripts" / "abbrev_gold_manifest.json"

RELATION_TYPES = (
  "acronym", "initialism", "abbreviation", "shorthand",
  "code", "nickname", "identifier", "unknown",
)

_SHORT = r"[A-ZÆØÅ][A-ZÆØÅ0-9.&-]{1,11}"
_LONG = r"[A-Za-zÆØÅæøå][\w'’.&/-]*(?:[ \t][\w'’.&/-]+){0,6}"

# (pattern_name, compiled regex, (short_group, long_group))
PATTERNS = [
  ("long_paren_short", re.compile(rf"({_LONG})[ \t]*\(({_SHORT})\)"), (2, 1)),
  ("short_paren_long", re.compile(rf"\b({_SHORT})[ \t]*\(({_LONG})\)"), (1, 2)),
  ("equals", re.compile(rf"\b({_SHORT})[ \t]*=[ \t]*({_LONG})"), (1, 2)),
  ("stands_for", re.compile(
    rf"\b({_SHORT})[ \t]+(?:står for|stands for|is short for|"
    rf"er (?:en )?forkortelse for)[ \t]+({_LONG})", re.IGNORECASE), (1, 2)),
  ("called", re.compile(
    rf"({_LONG})[ \t]*[,(]?[ \t]*(?:forkortet|forkortes(?: til)?|"
    rf"kalles(?: også)?|also known as|aka)[ \t]+({_SHORT})\b"), (2, 1)),
]

_PHONE = re.compile(r"^\+?\d[\d\s()-]{5,}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LETTERS = re.compile(r"[^\wæøåÆØÅ]", re.UNICODE)


def _letters(s: str) -> str:
  return _LETTERS.sub("", s).casefold()


def is_subsequence(short: str, long: str) -> bool:
  """Every letter of `short` appears in `long`, in order (CRAYN in Crayon)."""
  it = iter(_letters(long))
  return all(ch in it for ch in _letters(short))


def initials_of(long: str) -> str:
  return "".join(w[0] for w in re.split(r"[\s/-]+", long.strip()) if w).casefold()


# lowercase function words that mark the end of a definition tail
# ("SP = serviceprovider i NOCOS"); capitalized words ("Søk Og Redning") stay.
_TAIL_STOP = {"i", "på", "til", "fra", "med", "hos", "som", "er", "in", "on", "when"}


def _trim_tail(long: str) -> str:
  words = long.split()
  for n, w in enumerate(words):
    if n and w in _TAIL_STOP:
      return " ".join(words[:n])
  return long


def extract_candidates(text: str) -> list[dict]:
  """All explicit-pattern matches in one text. High recall by design; the only
  filter is a subsequence sanity check on the loose paren patterns."""
  out = []
  for name, rx, (si, li) in PATTERNS:
    for m in rx.finditer(text):
      short, long = m.group(si).strip(" .,"), m.group(li).strip(" .,")
      if name in ("equals", "stands_for"):
        long = _trim_tail(long)
      if len(_letters(short)) < 2 or len(long) <= len(short):
        continue
      if name.endswith("paren_short") or name.startswith("short_paren"):
        if not is_subsequence(short, long):
          continue
      out.append({
        "short_form": short,
        "long_form": long,
        "pattern": name,
        "excerpt": text[max(0, m.start() - 30):m.end() + 30].replace("\n", " ").strip(),
      })
  return out


def mine() -> int:
  if not FIXTURE.exists():
    print("MISSING fixture — run promotion_freeze.py first")
    return 1
  candidates: dict[tuple[str, str], dict] = {}
  n_scanned = n_holdout = 0
  with open_db(FIXTURE, readonly=True) as conn:
    for r in conn.execute(
      "SELECT id, source, thread_id, timestamp, content, subject FROM items"
    ):
      if split_of(r["source"], r["thread_id"], str(r["id"])) == "holdout":
        n_holdout += 1
        continue  # holdout evidence is reserved for the final eval
      n_scanned += 1
      text = " ".join(t for t in (r["subject"], r["content"]) if t)
      for c in extract_candidates(text):
        key = (c["short_form"].casefold(), c["long_form"].casefold())
        cur = candidates.setdefault(key, {
          "short_form": c["short_form"],
          "long_form": c["long_form"],
          "patterns": [],
          "evidence_items": [],
          "excerpts": [],
          "sources": [],
          "first_seen": r["timestamp"],
          "last_seen": r["timestamp"],
          "n_items": 0,
        })
        cur["n_items"] += 1
        if c["pattern"] not in cur["patterns"]:
          cur["patterns"].append(c["pattern"])
        if r["source"] not in cur["sources"]:
          cur["sources"].append(r["source"])
        if len(cur["evidence_items"]) < 5:
          cur["evidence_items"].append(str(r["id"]))
          cur["excerpts"].append(c["excerpt"][:160])
        ts = r["timestamp"] or ""
        cur["first_seen"] = min(cur["first_seen"] or ts, ts) or cur["first_seen"]
        cur["last_seen"] = max(cur["last_seen"] or ts, ts) or cur["last_seen"]

  with MINED.open("w") as f:
    for c in sorted(candidates.values(), key=lambda c: -c["n_items"]):
      f.write(json.dumps(c, ensure_ascii=False) + "\n")
  print(f"scanned {n_scanned:,} non-holdout items ({n_holdout:,} holdout skipped)")
  print(f"  {len(candidates):,} distinct (short, long) candidates -> {MINED}")
  by_pattern = Counter(p for c in candidates.values() for p in c["patterns"])
  for name, n in by_pattern.most_common():
    print(f"    {name}: {n}")
  return 0


def classify(alias: str, canonical: str, entity_type: str) -> tuple[str, str, int]:
  """(relation_type, note, needs_review) from shape alone. Conservative:
  anything not clearly identifier/initialism/abbreviation stays unknown with
  needs_review=1 for the human pass."""
  a, c = alias.strip(), canonical.strip()
  if _PHONE.match(a) or _EMAIL.match(a):
    return "identifier", "phone/email", 0
  if _letters(a) == _letters(c):
    return "unknown", "surface variant / NER noise, not an abbreviation", 1
  if _letters(a) == initials_of(c) and len(_letters(a)) >= 2:
    return "initialism", "letters match canonical initials", 1
  if entity_type == "person":
    # short forms of people are nicknames (jonna, Kim), not abbreviations,
    # even when they happen to be contractions of the name
    return "nickname", "person alias", 1
  if len(_letters(a)) < len(_letters(c)) and is_subsequence(a, c):
    return "abbreviation", "contraction of canonical", 1
  return "unknown", "", 1


def label() -> int:
  if not WORKSHEET.exists():
    print("MISSING worksheet — run promotion_freeze.py --worksheet first")
    return 1
  mined_by_short: dict[str, list[dict]] = {}
  if MINED.exists():
    for line in MINED.read_text().splitlines():
      c = json.loads(line)
      mined_by_short.setdefault(_letters(c["short_form"]), []).append(c)

  rows = list(csv.DictReader(WORKSHEET.open()))
  out_fields = [
    "alias", "canonical", "entity_type", "origin",
    "relation_type", "long_form", "context",
    "label_source", "needs_review", "evidence_items", "notes",
  ]
  n_mined = 0
  with GOLD.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=out_fields)
    w.writeheader()
    for r in rows:
      rel, note, review = classify(r["alias"], r["canonical"], r["entity_type"])
      hits = mined_by_short.get(_letters(r["alias"]), [])
      evidence = ";".join(h["evidence_items"][0] for h in hits[:3])
      if hits:
        n_mined += 1
        note = (note + "; " if note else "") + f"mined: {hits[0]['long_form'][:60]}"
      w.writerow({
        "alias": r["alias"], "canonical": r["canonical"],
        "entity_type": r["entity_type"], "origin": r["origin"],
        "relation_type": rel, "long_form": "", "context": "",
        "label_source": "heuristic", "needs_review": review,
        "evidence_items": evidence, "notes": note,
      })
  print(f"pre-labeled {len(rows)} worksheet rows -> {GOLD} ({n_mined} with mined evidence)")
  print("  review every needs_review=1 row, fill long_form/context, then run gold-manifest")
  return 0


def gold_manifest() -> int:
  if not GOLD.exists():
    print("MISSING gold CSV — run label (and review it) first")
    return 1
  rows = list(csv.DictReader(GOLD.open()))
  bad = [r["alias"] for r in rows if r["relation_type"] not in RELATION_TYPES]
  if bad:
    print(f"INVALID relation_type on {len(bad)} rows (e.g. {bad[:5]}) — fix before pinning")
    return 2
  unreviewed = sum(1 for r in rows if r["needs_review"] == "1")
  manifest = {
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "gold_csv": str(GOLD),
    "gold_sha256": hashlib.sha256(GOLD.read_bytes()).hexdigest(),
    "row_count": len(rows),
    "unreviewed_rows": unreviewed,
    "relation_type_counts": dict(Counter(r["relation_type"] for r in rows).most_common()),
    "label_source_counts": dict(Counter(r["label_source"] for r in rows).most_common()),
  }
  GOLD_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
  print(f"pinned gold set: {len(rows)} rows ({unreviewed} still needs_review)")
  print(f"  {manifest['relation_type_counts']}")
  print(f"  manifest -> {GOLD_MANIFEST.relative_to(_REPO)}")
  return 0


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("stage", choices=["mine", "label", "gold-manifest"])
  args = ap.parse_args()
  return {"mine": mine, "label": label, "gold-manifest": gold_manifest}[args.stage]()


if __name__ == "__main__":
  raise SystemExit(main())
