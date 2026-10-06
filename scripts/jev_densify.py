"""Jev A2 tasks 1 + 3: Jev rel-1 as relevance judge over the top-50 pool.

  .venv/bin/python scripts/jev_densify.py --db <fixture copy> validate   # task 1, 42 golds
  .venv/bin/python scripts/jev_densify.py --db <fixture copy> misses     # task 3, owner sheet

Read-only. Each query is re-parsed fresh (as `llm_judge_unjudged --rejudge-misses`
does), retrieved at depth 50 with junk excluded and recency_now = asked-on, and
every candidate scored with the shared rel-1 state/text so B4/B2 pairs are
cache hits. `misses` also runs the LLM judge lane (its own top-8 config, first
pass + all `--votes` verify votes counted) and writes a2_candidates.tsv.
"""
import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from autoresearch_retrieval import _load_gold  # noqa: E402
from llm_judge_unjudged import PROMPT, VERIFY_PROMPT  # noqa: E402

from yaams import jev  # noqa: E402
from yaams.cli._shared import _embed_config, _self_identities  # noqa: E402
from yaams.config import load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402
from yaams.enrich import Embedder  # noqa: E402
from yaams.retrieve import HybridQueryConfig, filter_results_by_entities, route  # noqa: E402
from yaams.retrieve import query as run_query  # noqa: E402
from yaams.retrieve.parse import parse_query  # noqa: E402
from yaams.retrieve.synonyms import normalize_synonym_groups  # noqa: E402
from yaams.synthesize.llm import llm_adapter_from_config  # noqa: E402
from yaams.time import parse_iso_datetime  # noqa: E402

GOLD_SQL = Path.home() / "brain/feed/eval/junk_gold_labels.sql"
# scripts/autoresearch_ideas.md:17 (jul01 review): 6 BAD_LABEL + 15 MECHANICAL_NOISE.
# Matched on casefolded, whitespace-normalised query text. The note names the
# "two keyword-pile queries" and the "verbatim BRKH message paste" without text;
# those three are the entries marked (inferred).
EXCLUDE = {
  "where does nina work": "BAD_LABEL", "hvor jobber nina": "BAD_LABEL",
  "what did nina say about summer vacation": "BAD_LABEL", "skatt": "BAD_LABEL",
  "feed firehose sync to vps over tailscale": "BAD_LABEL",
  "6117601": "NOISE", "torild01": "NOISE", "fbu": "NOISE", "vaktleder": "NOISE",
  "what happened today conversation message meeting": "NOISE",
  "sleep timeline append bug sheep": "NOISE",
  "møte mail kalender oppfølging incident service desk brkh familie konfirmasjon": "NOISE (inferred keyword pile)",
  "claude code video vision transcription pdf typst": "NOISE (inferred keyword pile)",
}


def norm(s):
  return " ".join(s.casefold().split())


def excluded(text):
  t = norm(text)
  if t.startswith("siden dette haster å få på plass"):
    return "NOISE (inferred BRKH paste)"
  return EXCLUDE.get(t)


class Ctx:
  def __init__(self, db):
    self.db = db
    self.cfg = load_config()
    rc = self.cfg.get("retrieve")
    self.syn = normalize_synonym_groups(rc.get("synonyms") if isinstance(rc, dict) else None)
    self.self_ids = _self_identities(self.cfg)
    self.emb = Embedder(**_embed_config(self.cfg), quiet=True)
    self.llm = llm_adapter_from_config(self.cfg)
    self.conn = open_db(db, readonly=True)

  def parse(self, text):
    c = open_db(self.db, readonly=True)  # parse_query touches the db; one conn per thread
    try:
      return parse_query(text, self.llm, c)
    finally:
      c.close()

  def retrieve(self, r, parsed, depth, jev_mode):
    sf = json.loads(r["source_filter"] or "[]") or None
    kw = dict(top_k=depth, source_filter=sf, synonym_groups=self.syn,
              since=parse_iso_datetime(r["since"]) if r["since"] else None,
              until=parse_iso_datetime(r["until"]) if r["until"] else None)
    if jev_mode:  # harness nojunk replay semantics
      kw.update(exclude_junk=True, recency_now=parse_iso_datetime(r["ts"]) if r["ts"] else None)
    else:  # llm_judge_unjudged's own config
      kw["top_k"] = max(r["top_k"] or 10, depth)
    q = route(parsed, HybridQueryConfig(**kw), self_identities=self.self_ids) if parsed else HybridQueryConfig(**kw)
    q.top_k = kw["top_k"]
    fts = " ".join(parsed.topic_terms) if parsed and parsed.topic_terms else r["text"]
    res = run_query(self.conn, fts, embedding=self.emb.embed_batch([r["text"]])[0], config=q)
    if parsed and q.entity_filter:
      res = filter_results_by_entities(res, self.conn, q.entity_filter)
    return res[:depth]

  def score(self, r, ids, tag):
    state = jev.rel_state(r["text"], (r["ts"] or "")[:10])
    return jev.noul(state, jev.rel_texts(self.conn, ids), jev.REL_CRITERION,
                    criterion_version="rel-1", tag=tag)


def auc(pos, neg):
  if not pos or not neg:
    return None
  wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
  return round(wins / (len(pos) * len(neg)), 3)


def validate(ctx, workers):
  gold, _, _ = _load_gold(ctx.conn)
  gold = sorted(gold, key=lambda g: g["query_id"])
  t0 = time.perf_counter()
  with ThreadPoolExecutor(workers) as ex:
    parsed = list(ex.map(lambda g: ctx.parse(g["text"]), gold))
  t_parse = time.perf_counter() - t0
  rows, pos, neg = [], [], []
  for g, p in zip(gold, parsed):
    ids = [x.id for x in ctx.retrieve(g, p, 50, True)]
    sc = ctx.score(g, ids, "jev_a2_validate")
    order = sorted((i for i in ids if i in sc), key=lambda i: -sc[i])
    gid = g["result_id"]
    in_pool = gid in ids
    jr = order.index(gid) + 1 if gid in order else None
    jr_worst = sum(1 for i in order if sc[i] >= sc[gid]) if jr else None
    served = len(sc) == len(ids)  # a partially scored pool would rank the gold wrongly
    if served and in_pool:
      pos.append(sc[gid])
    if served:
      neg += [sc[i] for i in ids if i != gid]
    rows.append({"query": g["text"], "kind": g["kind"], "fallback": bool(p and p.fallback_used),
                 "hybrid_rank": ids.index(gid) + 1 if in_pool else None, "jev_rank": jr,
                 "jev_rank_worst": jr_worst, "gold_noul": sc.get(gid), "missing": len(ids) - len(sc), "served": served})
  # owner junk labels as known negatives, each scored against its own query
  labels = re.findall(r"query_id='([^']+)' AND result_id='([0-9a-f]{64})'", GOLD_SQL.read_text())
  junk = []
  for qid, rid in labels:
    q = ctx.conn.execute("SELECT text, ts FROM queries WHERE id=?", (qid,)).fetchone()
    if q:
      s = ctx.score({"text": q["text"], "ts": q["ts"]}, [rid], "jev_a2_validate")
      junk.append((q["text"], rid[:12], s.get(rid)))
  all_rows = rows
  rows = [r for r in all_rows if r["served"]]  # metrics only over fully scored pools
  n = len(rows)
  in_pool = [r for r in rows if r["hybrid_rank"]]
  rep = {"n_golds": len(all_rows), "n_fully_scored": n,
         "blocked": [r["query"] for r in all_rows if not r["served"]],
         "parse_fallbacks": sum(r["fallback"] for r in all_rows),
         "gold_in_top50": len(in_pool),
         "jev_rank1": sum(r["jev_rank"] == 1 for r in rows), "jev_top3": sum(bool(r["jev_rank"]) and r["jev_rank"] <= 3 for r in rows),
         "hybrid_rank1": sum(r["hybrid_rank"] == 1 for r in rows),
         "hybrid_top3": sum(bool(r["hybrid_rank"]) and r["hybrid_rank"] <= 3 for r in rows),
         "auc_gold_vs_unlabeled": auc(pos, neg),
         "gold_noul_median": sorted(pos)[len(pos) // 2] if pos else None,
         "nongold_noul_median": sorted(neg)[len(neg) // 2] if neg else None,
         "owner_junk_labels": len(labels), "owner_junk_scored": len([j for j in junk if j[2] is not None]),
         "owner_junk_ge_0.5": sum(1 for j in junk if (j[2] or 0) >= 0.5),
         "auc_gold_vs_owner_junk": auc(pos, [j[2] for j in junk if j[2] is not None]),
         "parse_wall_s": round(t_parse, 1), "rows": all_rows, "owner_junk": junk}
  (jev.JEV_DIR / "a2_validate.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
  print(json.dumps({k: v for k, v in rep.items() if k not in ("rows", "owner_junk")}, indent=1))
  for r in all_rows:
    print(f"  {r['query'][:48]:48s} served={int(r['served'])} {r['kind']:10s} fb={int(r['fallback'])} hyb={r['hybrid_rank']} "
          f"jev={r['jev_rank']}-{r['jev_rank_worst']} noul={r['gold_noul']}")


def llm_judge(ctx, r, parsed, votes):
  """The rejudge lane: first-pass pick over its top-8, then all `votes` verify votes."""
  res = ctx.retrieve(r, parsed, 8, False)
  if not res:
    return None, None, "no results"
  asked = (r["ts"] or "")[:10]
  listing = "\n".join(
    f"{i}. [{x.kind}] {x.timestamp.date() if x.timestamp else '?'} {x.subject[:60]} :: {(x.content or '')[:160]}"
    for i, x in enumerate(res, 1))
  try:
    out = ctx.llm.complete(PROMPT.format(query=f"{r['text']}  (asked on {asked})" if asked else r["text"],
                                         results=listing), max_tokens=120).text
    m = re.search(r"\{.*\}", out, re.DOTALL)
    v = json.loads(m.group(0)) if m else {}
  except Exception as exc:  # noqa: BLE001
    return None, None, f"judge error {exc}"
  kind, rank = v.get("verdict"), (1 if v.get("verdict") == "hit" else v.get("rank"))
  if kind not in ("hit", "correction") or not rank or not 1 <= rank <= len(res):
    return None, None, kind or "unparsed"
  cand = res[rank - 1]
  vp = VERIFY_PROMPT.format(query=r["text"], kind=cand.kind, subject=cand.subject[:80],
                            content=(cand.content or "")[:300])
  yes = 0
  for _ in range(votes):
    try:
      o = ctx.llm.complete(vp, max_tokens=30).text
      mm = re.search(r"\{.*\}", o, re.DOTALL)
      yes += bool(mm and json.loads(mm.group(0)).get("correct") is True)
    except Exception:  # noqa: BLE001
      pass
  return cand, f"{yes}/{votes}", kind


def misses(ctx, workers, votes, skip_llm):
  rows = ctx.conn.execute("""
    SELECT q.id, q.text, q.top_k, q.source_filter, q.since, q.until, q.ts
    FROM queries q
    JOIN (SELECT query_id, MAX(id) AS mid FROM query_feedback WHERE kind != 'bad_result' GROUP BY query_id) last
      ON last.query_id = q.id
    JOIN query_feedback f ON f.id = last.mid
    WHERE f.kind = 'miss' AND COALESCE(q.results_returned,0) > 0
    ORDER BY q.id""").fetchall()
  rows = [dict(r) for r in rows]
  excl = [(r["text"], excluded(r["text"])) for r in rows if excluded(r["text"])]
  keep = [r for r in rows if not excluded(r["text"])]
  print(f"eligible {len(rows)}, excluded {len(excl)}, kept {len(keep)}")
  for t, why in excl:
    print(f"  excluded [{why}] {t[:90]!r}")
  t0 = time.perf_counter()
  with ThreadPoolExecutor(workers) as ex:
    parsed = list(ex.map(lambda r: ctx.parse(r["text"]), keep))
  t_parse = time.perf_counter() - t0
  t0 = time.perf_counter()
  top3 = []
  for r, p in zip(keep, parsed):
    ids = [x.id for x in ctx.retrieve(r, p, 50, True)]
    sc = ctx.score(r, ids, "jev_a2_misses")
    top3.append(sorted(((i, sc[i]) for i in ids if i in sc), key=lambda kv: -kv[1])[:3])
  t_jev = time.perf_counter() - t0
  t0 = time.perf_counter()
  if skip_llm:
    judged = [(None, None, "skipped")] * len(keep)
  else:
    with ThreadPoolExecutor(workers) as ex:
      judged = list(ex.map(lambda rp: llm_judge(ctx, rp[0], rp[1], votes), zip(keep, parsed)))
  t_llm = time.perf_counter() - t0

  def snip(i):
    t = jev.rel_texts(ctx.conn, [i]).get(i, "")
    return " / ".join(ln.strip() for ln in t.splitlines() if ln.strip())[:120]

  out = jev.JEV_DIR / "a2_candidates.tsv"
  with open(out, "w", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["n", "query", "asked_on",
                "jev1_id", "jev1_noul", "jev1_snippet", "jev2_id", "jev2_noul", "jev2_snippet",
                "jev3_id", "jev3_noul", "jev3_snippet", "llm_pick_id", "llm_pick_snippet",
                "llm_verdict", "llm_verify_votes", "fallback", "owner_pick", "query_id"])
    for n, (r, p, t3, (cand, votes_s, kind)) in enumerate(zip(keep, parsed, top3, judged), 1):
      cells = []
      for i, v in t3 + [("", "")] * (3 - len(t3)):
        cells += [i, v, snip(i) if i else ""]
      w.writerow([n, r["text"], (r["ts"] or "")[:10], *cells, cand.id if cand else "",
                  snip(cand.id) if cand else "", kind, votes_s or "", int(bool(p and p.fallback_used)),
                  "", r["id"]])
  print(f"wrote {len(keep)} rows -> {out}")
  print(json.dumps({"parse_wall_s": round(t_parse, 1), "jev_wall_s": round(t_jev, 1),
                    "llm_judge_wall_s": round(t_llm, 1),
                    "parse_fallbacks": sum(bool(p and p.fallback_used) for p in parsed),
                    "llm_verdicts": {k: sum(1 for j in judged if j[2] == k) for k in {j[2] for j in judged}},
                    "llm_verified": sum(1 for j in judged if j[1] and int(j[1].split("/")[0]) > votes // 2)}))


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True, help="a COPY of the fixture")
  ap.add_argument("mode", choices=["validate", "misses"])
  ap.add_argument("--workers", type=int, default=4)
  ap.add_argument("--votes", type=int, default=3)
  ap.add_argument("--skip-llm", action="store_true")
  a = ap.parse_args()
  ctx = Ctx(a.db)
  validate(ctx, a.workers) if a.mode == "validate" else misses(ctx, a.workers, a.votes, a.skip_llm)


if __name__ == "__main__":
  main()
