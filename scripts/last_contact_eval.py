"""Mechanical "last contact" eval: does retrieval find the latest conversation with a person?

  .venv/bin/python scripts/last_contact_eval.py build --db <participants fixture copy> --out lc.jsonl
  .venv/bin/python scripts/last_contact_eval.py run --cases lc.jsonl --db <fixture> [--promote-entities]

build: picks people (person entities with >= --min-msgs participant links and a multi-word
name, not the owner), dates each question one day after their latest message, parses
"when did I last speak with X?" / "når snakket jeg sist med X?" with the configured LLM
parser (as of that date), and records the acceptable answers: every item in the final
conversation (the latest participant item's thread, within 2 h before it).
run: replays every case through the harness path (route, hybrid, entity filter, eval depth
50) and reports hit@1, hit@5, MRR on the acceptable set. No labels, no gold set touched.
"""
import argparse
import json
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import autoresearch_retrieval as ar  # noqa: E402

from yaams.cli._shared import _embed_config, _self_identities  # noqa: E402
from yaams.config import load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402
from yaams.time import parse_iso_datetime  # noqa: E402

TEMPLATES = {"en": "when did I last speak with {}?", "nb": "når snakket jeg sist med {}?"}


def build(a):
  from yaams.retrieve.parse import parse_query
  from yaams.synthesize.llm import llm_adapter_from_config

  cfg = load_config()
  conn = open_db(a.db, readonly=True)
  selfs = {s.lower() for s in _self_identities(cfg)}
  people = conn.execute(
    "SELECT e.id, e.canonical_name, count(*) n FROM item_entities ie JOIN entities e ON e.id = ie.entity_id "
    "WHERE ie.source = 'participant' AND e.entity_type = 'person' AND e.canonical_name LIKE '% %' "
    "GROUP BY e.id HAVING n >= ? ORDER BY e.canonical_name", (a.min_msgs,)).fetchall()
  people = [p for p in people if p[1].lower() not in selfs and not any(s in p[1].lower() for s in selfs)]
  pick = random.Random(20261006).sample(people, min(a.n, len(people)))
  llm = llm_adapter_from_config(cfg)
  cases = []
  def with_owner(item_id):  # "spoke with" = the owner is a participant too (route's participant_filter)
    s, r = conn.execute("SELECT sender, recipients FROM items WHERE id=?", (item_id,)).fetchone()
    people = [s or ""] + (json.loads(r) if r and r.startswith("[") else [])
    return any(str(p).strip().lower() in selfs_all for p in people)

  selfs_all = selfs | {"me"}
  for eid, name, n in pick:
    direct = [r for r in conn.execute(
      "SELECT i.id, i.thread_id, i.timestamp FROM item_entities ie JOIN items i ON i.id = ie.item_id "
      "WHERE ie.entity_id = ? AND ie.source = 'participant' ORDER BY i.timestamp DESC", (eid,)) if with_owner(r[0])]
    if not direct:
      print(f"{name!r}: no direct conversation with the owner, skipped")
      continue
    last = direct[0]
    t_last = parse_iso_datetime(last[2])
    window_start = (t_last - timedelta(hours=2)).isoformat()
    ok = [r[0] for r in direct if r[1] == last[1] and window_start <= r[2] <= last[2]]
    ask = t_last + timedelta(days=1)
    for lang, tpl in TEMPLATES.items():
      text = tpl.format(name)
      parsed = parse_query(text, llm, conn, now=ask)
      cases.append({"query_id": f"lc:{eid}:{lang}", "person": name, "lang": lang, "text": text,
                    "ts": ask.isoformat(), "parsed_query": parsed.to_json(), "acceptable": ok,
                    "latest": last[0], "participant_msgs": n})
      print(f"{name!r} [{lang}] shape={json.loads(parsed.to_json())['shape']} "
            f"entities={json.loads(parsed.to_json())['entities']} ok={len(ok)}", flush=True)
  Path(a.out).write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n")
  print(f"{len(cases)} cases ({len(pick)} people) -> {a.out}")


def run(a):
  from yaams.enrich import Embedder
  from yaams.retrieve.synonyms import normalize_synonym_groups

  cfg = load_config()
  conn = open_db(a.db, readonly=True)
  if a.promote_entities:
    ar._load_promotions(conn)
  ar._OCCURRENCE_BROWSE = a.occurrence_browse
  rc = cfg.get("retrieve")
  syn = normalize_synonym_groups(rc.get("synonyms") if isinstance(rc, dict) else None)
  emb = Embedder(**_embed_config(cfg), quiet=True)
  holder = {}

  def capture(fn):
    def w(*x, **k):
      holder["res"] = fn(*x, **k)
      return holder["res"]
    return w

  ar.run_query = capture(ar.run_query)
  ar.filter_results_by_entities = capture(ar.filter_results_by_entities)
  cases = [json.loads(line) for line in open(a.cases)]
  rows = []
  for c in cases:
    row = {"query_id": c["query_id"], "text": c["text"], "parsed_query": c["parsed_query"],
           "source_filter": None, "since": None, "until": None, "ts": c["ts"], "result_id": c["latest"]}
    ar._replay_one(conn, emb, _self_identities(cfg), row, syn, exclude_junk=a.exclude_junk)
    ok = set(c["acceptable"])
    ids = [r.id for r in holder["res"]]
    rank = next((i for i, x in enumerate(ids, 1) if x in ok), None)
    rows.append((c, rank, rank != 1 and bool(ids) and _newer_link(conn, ids[0], c)))
  out = {}
  for lang in ("all", "en", "nb"):
    sel = [x for x in rows if lang == "all" or x[0]["lang"] == lang]
    rs = [r for _, r, _ in sel]
    out[lang] = {"n": len(rs), "hit@1": sum(r == 1 for r in rs) / len(rs),
                 "newer_link@1": sum(n for _, _, n in sel) / len(rs),
                 "hit@5": sum(bool(r) and r <= 5 for r in rs) / len(rs),
                 "mrr": sum(1 / r for r in rs if r) / len(rs)}
  print(json.dumps(out))
  if a.ranks_out:
    Path(a.ranks_out).write_text(json.dumps({c["query_id"]: r for c, r, _ in rows}, indent=1))


def _newer_link(conn, item_id, c):
  """Top-1 is not in the answer key but is linked to the person and newer than their last
  direct conversation (a note or meeting that mentions them): a key disagreement, not a miss."""
  row = conn.execute(
    "SELECT i.timestamp FROM items i JOIN item_entities ie ON ie.item_id = i.id "
    "JOIN entities e ON e.id = ie.entity_id WHERE i.id = ? AND e.canonical_name = ?",
    (item_id, c["person"])).fetchone()
  return bool(row) and row[0] > conn.execute(
    "SELECT timestamp FROM items WHERE id = ?", (c["latest"],)).fetchone()[0]


def main():
  ap = argparse.ArgumentParser()
  sub = ap.add_subparsers(dest="cmd", required=True)
  b = sub.add_parser("build")
  b.add_argument("--db", required=True)
  b.add_argument("--out", required=True)
  b.add_argument("--n", type=int, default=30)
  b.add_argument("--min-msgs", type=int, default=15)
  r = sub.add_parser("run")
  r.add_argument("--cases", required=True)
  r.add_argument("--db", required=True)
  r.add_argument("--promote-entities", action="store_true")
  r.add_argument("--exclude-junk", action="store_true")
  r.add_argument("--ranks-out")
  r.add_argument("--occurrence-browse", action="store_true")
  a = ap.parse_args()
  build(a) if a.cmd == "build" else run(a)


if __name__ == "__main__":
  main()
