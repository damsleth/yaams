"""Pick the noul question format for Jeff on a small probe set, before any full run.

  .venv/bin/python scripts/jeff_format_probe.py --live-db <copy> --fixture <copy>

F1 = the Jev plan's format (candidate in `instructions`, claim in `criteria.true`),
F2 = the yes/no question then the candidate as one `instructions` string,
F3 = structured `instructions` {question, <field>: candidate}.
Junk probe: 40 Sonnet-consensus rows (20 JUNK / 20 KEEP, seed 11) that are NOT
on the A1 owner sheet. Relevance probe: the 3 B4 pilot queries, each gold
against 20 random non-junk items (seed 11), reported as how many random items
score >= the gold. The winner is used, unchanged, for the full runs.
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from jev_junk_pass import CRITERION as JUNK_CLAIM  # noqa: E402
from jev_junk_pass import block, rubric
from junk_sonnet_pass import load_ckpt  # noqa: E402

from yaams import jev  # noqa: E402
from yaams.db import open_db  # noqa: E402

JUNK_Q = "Is the TARGET message junk, carrying no retrievable content on its own?"
REL_Q = "Does this item contain evidence that answers the question or directly helps answer it?"


def questions(fmt, texts, claim, q, field):
  if fmt == "F1":
    return texts, claim
  if fmt == "F2":
    return {k: f"{q}\n\n{t}" for k, t in texts.items()}, None
  return {k: {"question": q, field: t} for k, t in texts.items()}, None


def auc(pos, neg):
  if not pos or not neg:
    return float("nan")
  return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def score(state, texts, claim, fmt, q, field, version):
  qs, crit = questions(fmt, texts, claim, q, field)
  if fmt == "F3":  # noul() sends str instructions; build the structured request directly
    out = {}
    body = {"model": jev.MODEL, "state": state,
            "questions": {f"q{i}": {"type": "noul", "instructions": v} for i, v in enumerate(qs.values())}}
    resp = jev._post(body, f"jeff_probe_{version}") or {}
    for i, k in enumerate(qs):
      a = (resp.get("answers") or {}).get(f"q{i}")
      if a:
        out[k] = a["noul"]
    return out
  return jev.noul(state, qs, crit, criterion_version=f"{version}-{fmt}", tag=f"jeff_probe_{version}",
                  use_cache=False)


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--live-db", required=True)
  ap.add_argument("--fixture", required=True)
  a = ap.parse_args()
  random.seed(11)

  sheet = {r["item_id"] for r in csv.DictReader(open(jev.JEV_DIR.parent / "jev/a1_disagreements.tsv"),
                                                delimiter="\t")}
  r1, r2 = load_ckpt(1), load_ckpt(2)
  cons = [i for i in r1 if i in r2 and r1[i] == r2[i] and i not in sheet]
  jk = random.sample([i for i in cons if r1[i] == "JUNK"], 20)
  kp = random.sample([i for i in cons if r1[i] == "KEEP"], 20)
  conn = open_db(a.live_db, readonly=True)
  rows = {r["id"]: dict(r) for r in conn.execute(
    f"SELECT id, source, thread_id, timestamp, content FROM items WHERE id IN ({','.join('?' * 40)})", jk + kp)}
  jtexts = {i: block(conn, rows[i]) for i in jk + kp}
  jstate = {"task": rubric("en"), "owner_context": "personal search index over Kim's messages"}

  fx = open_db(a.fixture, readonly=True)
  pilot = [json.loads(line) for line in open(jev.JEV_DIR.parent / "jev/b4_pilot.jsonl")]
  nonjunk = [r[0] for r in fx.execute("SELECT id FROM items WHERE junk_reason IS NULL")]
  q = fx.execute("SELECT q.id, q.text, q.ts FROM queries q WHERE q.id IN (?,?,?)",
                 [p["query_id"] for p in pilot]).fetchall()
  qinfo = {r[0]: (r[1], r[2][:10]) for r in q}

  for fmt in ("F1", "F2", "F3"):
    js = score(jstate, jtexts, JUNK_CLAIM, fmt, JUNK_Q, "message", "junk")
    ja = auc([js[i] for i in jk if i in js], [js[i] for i in kp if i in js])
    ra = []
    for p in pilot:
      text, ts = qinfo[p["query_id"]]
      rand = random.Random(11).sample(nonjunk, 20)
      ids = [p["gold_id"]] + rand
      rs = score(jev.rel_state(text, ts), jev.rel_texts(fx, ids), jev.REL_CRITERION, fmt, REL_Q, "item", "rel")
      g = rs.get(p["gold_id"])
      neg = [rs[i] for i in rand if i in rs]
      ra.append((text, g, sum(1 for n in neg if g is not None and n >= g)))
    print(f"{fmt}: junk AUC {ja:.3f} (JUNK mean {sum(js[i] for i in jk) / 20:.2f}, KEEP mean "
          f"{sum(js[i] for i in kp) / 20:.2f}) | relevance: " +
          "; ".join(f"{t[:22]!r} gold {g if g is None else round(g, 2)}, {n}/20 random >= gold" for t, g, n in ra),
          flush=True)


if __name__ == "__main__":
  main()
