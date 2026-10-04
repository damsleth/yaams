"""v3.1 junk decision rule: candidate vs incumbent on test-2, thresholds from the dev labels.

  .venv/bin/python scripts/junk_decide.py --candidate cand.jsonl --incumbent jeff.jsonl
  .venv/bin/python scripts/junk_decide.py --selftest

Score files: jsonl lines {"id"|"item_id": ..., "p_junk": float} covering the 200 dev
rows and the 250 test-2 rows. Run it ONCE per finalist: it reads test-2.

Rule (pre-registered, .plans v3.1 + amendment 2026-10-04):
  tau per model = median of 5-fold CV taus on the dev labels, each fold's tau the
  lowest threshold that keeps >= --keep-frac of that fold's keeps.
  On test-2: (1) sanity: keeps lost(candidate) <= keeps lost(incumbent) + 1;
  (2) primary: one-sided 90% paired-bootstrap lower bound of
      junk caught(candidate) - junk caught(incumbent) > -0.05.
"""
import argparse
import csv
import json
import random
from pathlib import Path

EVAL = Path.home() / "brain/feed/eval/jeff"
DEV = EVAL / "owner_junk_labels_200_2026-10-02.tsv"
TEST2 = EVAL / "owner_junk_labels_test2_250_2026-10-04.tsv"


def labels(path):
  rs = list(csv.DictReader(open(path), delimiter="\t"))
  col = next(c for c in rs[0] if c.startswith("owner_verdict"))
  return {r["item_id"]: r[col] in ("J", "JUNK") for r in rs if r[col]}


def scores(path):
  out = {}
  for line in open(path):
    r = json.loads(line)
    out[str(r.get("item_id") or r["id"]).split(":")[-1]] = float(r["p_junk"])
  return out


def tau_for(s, lab, ids, keep_frac):
  keeps = sorted(s[i] for i in ids if not lab[i])
  if not keeps:
    return 0.5
  k = max(1, -(-int(len(keeps) * keep_frac * 1000) // 1000))  # ceil without float drift
  return keeps[k - 1] + 1e-9  # junk iff p >= tau: the k lowest-scoring keeps stay below


def cv_tau(s, lab, keep_frac, folds=5, seed=7):
  ids = sorted(lab)
  random.Random(seed).shuffle(ids)
  taus = sorted(tau_for(s, lab, [i for j, i in enumerate(ids) if j % folds != f], keep_frac) for f in range(folds))
  return taus[len(taus) // 2]


def decide(cand, inc, dev, test, keep_frac=0.8, boot=10000, seed=11):
  tc, ti = cv_tau(cand, dev, keep_frac), cv_tau(inc, dev, keep_frac)
  junk = [i for i in test if test[i]]
  keep = [i for i in test if not test[i]]
  caught = lambda s, t, i: s[i] >= t  # noqa: E731
  lost_c = sum(caught(cand, tc, i) for i in keep)
  lost_i = sum(caught(inc, ti, i) for i in keep)
  diffs = [caught(cand, tc, i) - caught(inc, ti, i) for i in junk]
  point = sum(diffs) / len(diffs)
  rng = random.Random(seed)
  bs = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(boot))
  lb = bs[int(boot * 0.10)]
  ok1, ok2 = lost_c <= lost_i + 1, lb > -0.05
  return {"tau_candidate": tc, "tau_incumbent": ti, "test_junk": len(junk), "test_keeps": len(keep),
          "junk_caught_candidate": sum(caught(cand, tc, i) for i in junk) / len(junk),
          "junk_caught_incumbent": sum(caught(inc, ti, i) for i in junk) / len(junk),
          "diff_point": point, "diff_lb90": lb, "keeps_lost_candidate": lost_c, "keeps_lost_incumbent": lost_i,
          "sanity_keeps_ok": ok1, "primary_ok": ok2, "ship_candidate": ok1 and ok2}


def selftest():
  rng = random.Random(3)
  dev = {f"d{i}": i % 8 != 0 for i in range(200)}
  test = {f"t{i}": i % 25 != 0 for i in range(250)}
  base = {i: (0.9 if j else 0.2) + rng.uniform(-0.15, 0.15) for i, j in {**dev, **test}.items()}
  same = decide(base, base, dev, test)
  assert same["diff_point"] == 0 and same["ship_candidate"], same
  worse = {i: (v - 0.5 if i.startswith("t") and j else v) for (i, v), j in zip(base.items(), {**dev, **test}.values())}
  bad = decide(worse, base, dev, test)
  assert not bad["primary_ok"] and not bad["ship_candidate"] and bad["diff_point"] < -0.1, bad
  print("selftest ok")


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--candidate")
  ap.add_argument("--incumbent")
  ap.add_argument("--keep-frac", type=float, default=0.8)
  ap.add_argument("--selftest", action="store_true")
  a = ap.parse_args()
  if a.selftest:
    return selftest()
  dev, test = labels(DEV), labels(TEST2)
  cand, inc = scores(a.candidate), scores(a.incumbent)
  missing = [i for i in [*dev, *test] if i not in cand or i not in inc]
  if missing:
    raise SystemExit(f"{len(missing)} labelled rows lack scores, e.g. {missing[:3]}")
  print(json.dumps(decide(cand, inc, dev, test, a.keep_frac), indent=1))


if __name__ == "__main__":
  main()
