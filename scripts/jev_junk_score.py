"""Jev A1 scorer: agreement and calibration of Jev junk nouls vs Sonnet.

  .venv/bin/python scripts/jev_junk_score.py                        # report, both variants
  .venv/bin/python scripts/jev_junk_score.py --sheet --db <copy.db> # + 50-row owner sheet

Consensus = rows where Sonnet run1 == run2. JUNK is the positive class.
"""
import argparse
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from jev_usage import summarize  # noqa: E402
from junk_sonnet_pass import context, load_ckpt  # noqa: E402

from yaams.jev import JEV_DIR  # noqa: E402


def kappa(pairs):
  pairs = list(pairs)
  n = len(pairs)
  if not n:
    return float("nan")
  po = sum(a == b for a, b in pairs) / n
  pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
  pe = pa * pb + (1 - pa) * (1 - pb)
  return (po - pe) / (1 - pe) if pe < 1 else 1.0


def group(source):
  return "teams*" if source.startswith("teams") else source


def load(variant):
  return [json.loads(line) for line in open(JEV_DIR / f"junk-{variant}.jsonl")]


def report(variant, rows, r1, r2):
  j = lambda v: v == "JUNK"  # noqa: E731
  scope = [r for r in rows if not r["owner_gold"] and r["item_id"] in r1 and r["item_id"] in r2]
  cons = [(r, j(r1[r["item_id"]])) for r in scope if r1[r["item_id"]] == r2[r["item_id"]]]
  print(f"\n=== variant {variant}: {len(scope)} scored scope rows, {len(cons)} consensus "
        f"({sum(c for _, c in cons)} JUNK)")
  k_self = kappa((j(r1[r["item_id"]]), j(r2[r["item_id"]])) for r in scope)
  k05 = kappa((r["noul"] >= 0.5, c) for r, c in cons)
  print(f"kappa Jev@0.5 vs consensus  {k05:.3f}")
  print(f"kappa Sonnet run1 vs run2    {k_self:.3f}  (gate: Jev >= {k_self - 0.05:.3f})")
  # like-for-like: consensus rows are the easy ones, so also score Jev on ALL rows
  for name, run in (("run1", r1), ("run2", r2)):
    k = kappa((r["noul"] >= 0.5, j(run[r["item_id"]])) for r in scope)
    print(f"kappa Jev@0.5 vs {name} (all rows) {k:.3f}")
  best = max((kappa((r["noul"] >= t / 100, c) for r, c in cons), t / 100) for t in range(5, 96))
  print(f"best tau {best[1]:.2f} -> kappa {best[0]:.3f}  (reported only; verdict uses 0.5)")
  jevj = sum(r["noul"] >= 0.5 for r, _ in cons)
  print(f"Jev@0.5 JUNK on consensus rows: {jevj}/{len(cons)}")

  print("\nreliability (consensus rows): bucket  n  mean_noul  frac_JUNK")
  buckets = defaultdict(list)
  for r, c in cons:
    buckets[min(int(r["noul"] * 10), 9)].append((r["noul"], c))
  ece, fracs = 0.0, []
  for b in range(10):
    xs = buckets.get(b, [])
    if not xs:
      print(f"  [{b / 10:.1f},{(b + 1) / 10:.1f})  0")
      continue
    m = sum(x for x, _ in xs) / len(xs)
    f = sum(c for _, c in xs) / len(xs)
    ece += len(xs) / len(cons) * abs(m - f)
    if len(xs) >= 50:
      fracs.append(f)
    print(f"  [{b / 10:.1f},{(b + 1) / 10:.1f})  {len(xs):5d}  {m:.3f}  {f:.3f}")
  mono = all(a <= b for a, b in zip(fracs, fracs[1:]))
  print(f"ECE {ece:.3f}   monotonic over buckets with n>=50: {mono}")

  for key, fn in (("source", lambda r: group(r["source"])), ("lang", lambda r: r["lang"])):
    print(f"\nby {key}: group  n_cons  kappa@0.5  sonnet_self_kappa")
    for gname in sorted({fn(r) for r in scope}):
      c = [(r, x) for r, x in cons if fn(r) == gname]
      s = [r for r in scope if fn(r) == gname]
      ks = kappa((j(r1[r["item_id"]]), j(r2[r["item_id"]])) for r in s)
      print(f"  {gname:8s} {len(c):6d}  {kappa((r['noul'] >= 0.5, x) for r, x in c):.3f}  {ks:.3f}")

  og = [r["noul"] for r in rows if r["owner_gold"]]
  print(f"\nowner junk golds: {sum(x >= 0.5 for x in og)}/{len(og)} >= 0.5, "
        f"nouls {sorted(og)}")
  u = summarize(f"jev_a1_{variant}").get(f"jev_a1_{variant}", {})
  print(f"cost ${u.get('dollars')}  requests {u.get('requests')}  p50 {u.get('p50_ms')} ms  "
        f"p95 {u.get('p95_ms')} ms  tokens real {u.get('input_tokens')} est {u.get('est_tokens')}")
  return {"variant": variant, "kappa": k05, "cons": cons}


def sheet(res, r1, db):
  from yaams.db import open_db
  conn = open_db(db, readonly=True)
  random.seed(7)
  out = JEV_DIR / "a1_disagreements.tsv"
  picked = []
  for direction, want_jev in (("jev_JUNK_sonnet_KEEP", True), ("jev_KEEP_sonnet_JUNK", False)):
    pool = [r for r, c in res["cons"] if (r["noul"] >= 0.5) == want_jev and c != want_jev]
    by = defaultdict(list)
    for r in pool:
      by[r["lang"]].append(r)
    langs = sorted(by)
    take = {lg: 25 // len(langs) + (1 if i < 25 % len(langs) else 0) for i, lg in enumerate(langs)}
    for lg in langs:
      picked += [(direction, r) for r in random.sample(by[lg], min(take[lg], len(by[lg])))]
  with open(out, "w", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["n", "direction", "lang", "source", "jev_noul", "sonnet", "prev", "target", "next",
                "owner_verdict (JUNK/KEEP)", "item_id"])
    for n, (d, r) in enumerate(picked, 1):
      it = dict(conn.execute("SELECT id, source, thread_id, timestamp, content FROM items WHERE id=?",
                             (r["item_id"],)).fetchone())
      p, nx = context(conn, it)
      flat = lambda s: " / ".join(ln.strip() for ln in s.strip().splitlines() if ln.strip())  # noqa: E731
      w.writerow([n, d, r["lang"], r["source"], r["noul"], r1[r["item_id"]], flat(p),
                  flat(it["content"]), flat(nx), "", r["item_id"]])
  print(f"\nsheet ({res['variant']}, {len(picked)} rows) -> {out}")


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--variant", default="en,nb")
  ap.add_argument("--sheet", action="store_true")
  ap.add_argument("--db", help="db copy for sheet text")
  a = ap.parse_args()
  r1, r2 = load_ckpt(1), load_ckpt(2)
  res = [report(v, load(v), r1, r2) for v in a.variant.split(",")]
  if a.sheet:
    sheet(max(res, key=lambda x: x["kappa"]), r1, a.db)


if __name__ == "__main__":
  main()
