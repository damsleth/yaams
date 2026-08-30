#!/usr/bin/env python3
"""Freeze & audit the promotion-enrichment scenario (agentisk plan, PR 1).

Snapshots the live DB with SQLite backup semantics (safe under WAL/litestream,
unlike a raw file copy) and writes a non-sensitive scenario manifest
(`scripts/promotion_scenario.json`). The manifest pins everything the
YAAMS -> ledger enrichment loop must hold constant: schema version, fixture
hash, item/source/candidate counts, ingestion cursor, gold-feedback hash and
code/config identities. Analogous to `autoresearch_freeze.py --check`, a
different snapshot is a different, non-comparable campaign.

Usage:
    .venv/bin/python scripts/promotion_freeze.py              # freeze live -> fixture + manifest
    .venv/bin/python scripts/promotion_freeze.py --check      # verify fixture matches manifest
    .venv/bin/python scripts/promotion_freeze.py --report     # print baseline report from manifest
    .venv/bin/python scripts/promotion_freeze.py --worksheet  # export unlabeled abbreviation worksheet

The fixture and the worksheet contain private data and live under ~/brain,
outside git. The manifest contains only counts, hashes and identifiers.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from autoresearch_freeze import _gold_hash  # noqa: E402

from yaams.config import get_db_path, load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402
from yaams.schema import SCHEMA_VERSION  # noqa: E402

FIXTURE = Path.home() / "brain" / "promotion_fixture.db"
MANIFEST = _REPO / "scripts" / "promotion_scenario.json"
WORKSHEET = Path.home() / "brain" / "promotion_abbrev_worksheet.csv"
LEDGER_REPO = Path.home() / "code" / "cognitive-ledger"

_SECRET_KEY = re.compile(r"token|secret|password|passphrase|credential|api_key", re.I)


def _strip_secrets(obj):
  """Drop secret-bearing keys recursively so the config hash is non-sensitive."""
  if isinstance(obj, dict):
    return {k: _strip_secrets(v) for k, v in obj.items() if not _SECRET_KEY.search(str(k))}
  if isinstance(obj, list):
    return [_strip_secrets(v) for v in obj]
  return obj


def _sha256_file(path: Path) -> str:
  h = hashlib.sha256()
  with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
      h.update(chunk)
  return h.hexdigest()


def _git_head(repo: Path) -> str:
  try:
    out = subprocess.run(
      ["git", "-C", str(repo), "rev-parse", "HEAD"],
      capture_output=True, text=True, timeout=10,
    )
    return out.stdout.strip() or "unknown"
  except Exception:
    return "unknown"


def _candidate_hash(conn: sqlite3.Connection) -> str:
  """Stable hash over (id, status) of every promotion candidate — detects any
  later status drift against the frozen baseline evidence."""
  rows = sorted(
    (r["id"], r["status"])
    for r in conn.execute("SELECT id, status FROM promotion_candidates")
  )
  return hashlib.sha1(json.dumps(rows).encode()).hexdigest()


def _is_short_single_token(alias: str) -> bool:
  a = alias.strip()
  return 2 <= len(a) <= 12 and not any(ch.isspace() for ch in a)


def _alias_rows(conn: sqlite3.Connection, cfg: dict) -> list[dict]:
  """All alias relations from DB entities + config dictionary, deduped on
  (alias, canonical). High recall on purpose: nicknames, phone numbers and
  identifiers stay in; classification happens at labeling, not discovery."""
  seen: dict[tuple[str, str], dict] = {}

  def add(alias: str, canonical: str, etype: str, origin: str) -> None:
    alias = str(alias).strip()
    if not alias:
      return
    key = (alias, canonical)
    if key in seen:
      if origin not in seen[key]["origin"]:
        seen[key]["origin"] += f"+{origin}"
      return
    seen[key] = {
      "alias": alias, "canonical": canonical, "entity_type": etype, "origin": origin,
    }

  for r in conn.execute(
    "SELECT canonical_name, entity_type, aliases FROM entities "
    "WHERE aliases IS NOT NULL AND aliases != ''"
  ):
    try:
      aliases = json.loads(r["aliases"])
    except (TypeError, ValueError):
      continue
    for a in aliases if isinstance(aliases, list) else []:
      add(a, r["canonical_name"], r["entity_type"], "entity")

  for entry in (cfg.get("entities") or {}).get("dictionary") or []:
    for a in entry.get("aliases") or []:
      add(a, str(entry.get("canonical", "")), str(entry.get("type", "")), "config")

  return list(seen.values())


def _manifest_stats(conn: sqlite3.Connection, cfg: dict) -> dict:
  one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
  gold_hash, n_gold, n_corr = _gold_hash(conn)
  aliases = _alias_rows(conn, cfg)
  dictionary = (cfg.get("entities") or {}).get("dictionary") or []
  return {
    "schema_version": SCHEMA_VERSION,
    "schema_migrations_head": one("SELECT MAX(name) FROM schema_migrations"),
    "item_count": one("SELECT COUNT(*) FROM items"),
    "max_ingested_at": one("SELECT MAX(ingested_at) FROM items"),
    "max_item_rowid": one("SELECT MAX(rowid) FROM items"),
    "ingestion_cursor": list(
      conn.execute(
        "SELECT ingested_at, id FROM items ORDER BY ingested_at DESC, id DESC LIMIT 1"
      ).fetchone() or (None, None)
    ),
    "source_counts": dict(
      conn.execute("SELECT source, COUNT(*) FROM items GROUP BY source ORDER BY source")
    ),
    "entity_count": one("SELECT COUNT(*) FROM entities"),
    "entities_with_aliases": one(
      "SELECT COUNT(*) FROM entities WHERE aliases IS NOT NULL AND aliases NOT IN ('', '[]')"
    ),
    "alias_relation_count": len(aliases),
    "short_alias_count": sum(1 for a in aliases if _is_short_single_token(a["alias"])),
    "config_dictionary_entries": len(dictionary),
    "candidate_status_counts": dict(
      conn.execute("SELECT status, COUNT(*) FROM promotion_candidates GROUP BY status ORDER BY status")
    ),
    "candidate_hash": _candidate_hash(conn),
    "query_count": one("SELECT COUNT(*) FROM queries"),
    "gold_hash": gold_hash,
    "gold_queries": n_gold,
    "corrections": n_corr,
  }


def freeze() -> int:
  cfg = load_config()
  live = get_db_path(cfg)
  FIXTURE.parent.mkdir(exist_ok=True)
  src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
  dst = sqlite3.connect(FIXTURE)
  try:
    src.backup(dst)
  finally:
    dst.close()
    src.close()

  with open_db(FIXTURE, readonly=True) as conn:
    stats = _manifest_stats(conn, cfg)
  manifest = {
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "fixture": str(FIXTURE),
    "source_db": str(live),
    "fixture_sha256": _sha256_file(FIXTURE),
    "yaams_commit": _git_head(_REPO),
    "ledger_commit": _git_head(LEDGER_REPO),
    "config_hash": hashlib.sha256(
      json.dumps(_strip_secrets(cfg), sort_keys=True, default=str).encode()
    ).hexdigest(),
    **stats,
  }
  MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
  print(f"froze {live}\n  -> {FIXTURE}")
  print(f"  manifest -> {MANIFEST.relative_to(_REPO)}")
  report()
  return 0


def check() -> int:
  if not (FIXTURE.exists() and MANIFEST.exists()):
    print("MISSING fixture or manifest — run freeze first")
    return 1
  manifest = json.loads(MANIFEST.read_text())
  problems = []
  if _sha256_file(FIXTURE) != manifest["fixture_sha256"]:
    problems.append("fixture_sha256")
  with open_db(FIXTURE, readonly=True) as conn:
    gold_hash, _, _ = _gold_hash(conn)
    if gold_hash != manifest["gold_hash"]:
      problems.append("gold_hash")
    if _candidate_hash(conn) != manifest["candidate_hash"]:
      problems.append("candidate_hash")
    n_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if n_items != manifest["item_count"]:
      problems.append("item_count")
  if problems:
    print(f"MISMATCH: {', '.join(problems)} — the scenario has drifted; re-freeze deliberately")
    return 2
  print(f"OK: fixture matches manifest ({manifest['item_count']} items, "
        f"sha256={manifest['fixture_sha256'][:12]})")
  return 0


def report() -> int:
  if not MANIFEST.exists():
    print("MISSING manifest — run freeze first")
    return 1
  m = json.loads(MANIFEST.read_text())
  cand = m["candidate_status_counts"]
  print(f"""
Promotion scenario baseline — frozen {m['created_at']}
  fixture           {m['fixture']} (sha256 {m['fixture_sha256'][:12]})
  schema            v{m['schema_version']} ({m['schema_migrations_head']})
  yaams commit      {m['yaams_commit'][:12]}   ledger commit {m['ledger_commit'][:12]}
  items             {m['item_count']:,} (max ingested_at {m['max_ingested_at']})
  sources           {', '.join(f'{k}={v}' for k, v in m['source_counts'].items())}
  entities          {m['entity_count']:,} ({m['entities_with_aliases']} with aliases, \
{m['alias_relation_count']} alias relations, {m['short_alias_count']} short single-token)
  config dictionary {m['config_dictionary_entries']} entries
  candidates        {sum(cand.values())} total ({', '.join(f'{k}={v}' for k, v in cand.items())})
  queries           {m['query_count']} logged, {m['gold_queries']} gold ({m['corrections']} corrections)
""".rstrip())
  return 0


def worksheet() -> int:
  """Export every alias relation (short single-token first) as an unlabeled
  abbreviation gold-set worksheet. Labels are filled in by a human; this
  script never mutates entities or writes notes."""
  if not FIXTURE.exists():
    print("MISSING fixture — run freeze first")
    return 1
  cfg = load_config()
  with open_db(FIXTURE, readonly=True) as conn:
    rows = _alias_rows(conn, cfg)
  short = [r for r in rows if _is_short_single_token(r["alias"])]
  short.sort(key=lambda r: (r["alias"].lower(), r["canonical"].lower()))
  with WORKSHEET.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=[
      "alias", "canonical", "entity_type", "origin",
      # unlabeled columns, filled by human review:
      "relation_type", "long_form", "context", "notes",
    ])
    w.writeheader()
    for r in short:
      w.writerow({**r, "relation_type": "", "long_form": "", "context": "", "notes": ""})
  print(f"wrote {len(short)} short single-token alias candidates -> {WORKSHEET}")
  print(f"  (from {len(rows)} total alias relations; worksheet is private, outside git)")
  return 0


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--check", action="store_true")
  ap.add_argument("--report", action="store_true")
  ap.add_argument("--worksheet", action="store_true")
  args = ap.parse_args()
  if args.check:
    return check()
  if args.report:
    return report()
  if args.worksheet:
    return worksheet()
  return freeze()


if __name__ == "__main__":
  raise SystemExit(main())
