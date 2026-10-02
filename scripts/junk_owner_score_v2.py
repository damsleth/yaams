"""Junk v2 report: pick the fine-tuned threshold on the owner's tune half (n 1-100),
apply it once to the test half (n 101-200), and compare with every other judge."""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from jev_junk_score import kappa  # noqa: E402
from junk_sonnet_pass import load_ckpt  # noqa: E402

E = Path.home() / "brain/feed/eval"
rows = list(csv.DictReader(open(E / "jeff/ft/junk-v1/sample_sheet.tsv"), delimiter="\t"))
owner = {r["item_id"]: r["owner_verdict (J/K)"] == "J" for r in rows}
half = {r["item_id"]: "tune" if int(r["n"]) <= 100 else "test" for r in rows}
r1, r2 = load_ckpt(1), load_ckpt(2)


def preds(path):
  out = {}
  for p in map(json.loads, open(path)):
    out[p["id"].split(":", 1)[1]] = p["probabilities"][1]
  return out


v1 = preds(E / "jeff/ft/junk-v1/results/junk-v1-sample_unlabeled.predictions.jsonl")
v2 = {**preds(E / "jeff/ft/junk-v2/results/junk-v2-sample_tune.predictions.jsonl"),
      **preds(E / "jeff/ft/junk-v2/results/junk-v2-sample_test.predictions.jsonl")}
relabel = {}
for line in open(E / "jeff/relabel-owner-v1/all/sonnet_verdicts_run1.jsonl"):
  relabel.update(json.loads(line))
jev_nb = {r["item_id"]: r["noul"] for r in map(json.loads, open(E / "jev/junk-nb.jsonl"))}

tune = [i for i in owner if half[i] == "tune"]
tau = max((kappa((v2[i] >= t / 100, owner[i]) for i in tune), t / 100) for t in range(5, 96))[1]


def report(name, pred, ids):
  n = len(ids)
  nj = sum(owner[i] for i in ids)
  acc = sum(pred[i] == owner[i] for i in ids) / n
  jr = sum(pred[i] for i in ids if owner[i]) / nj
  kept = sum(not pred[i] for i in ids if not owner[i])
  hidden = sum(pred[i] for i in ids)
  print(f"  {name:38s} acc {acc:.3f}  kappa {kappa((pred[i], owner[i]) for i in ids):6.3f}  "
        f"junk caught {jr:.2f}  owner-keeps kept {kept}/{n - nj}  hides {hidden}/{n}")


judges = {
  "Sonnet consensus (live db today)": {i: r1[i] == r2[i] == "JUNK" for i in owner},
  "Jev nb @0.5": {i: jev_nb[i] >= 0.5 for i in owner},
  "Jeff fine-tuned v1 @0.5": {i: v1[i] >= 0.5 for i in owner},
  "owner-bar relabel (v2 training labels)": {i: relabel.get(i) == "JUNK" for i in owner},
  "Jeff fine-tuned v2 @0.5": {i: v2[i] >= 0.5 for i in owner},
  f"Jeff fine-tuned v2 @{tau:.2f} (tau from tune)": {i: v2[i] >= tau for i in owner},
  "all-JUNK baseline": {i: True for i in owner},
}
for split in ("test", "tune", "all"):
  ids = [i for i in owner if split == "all" or half[i] == split]
  label = {"test": "TEST half (n 101-200, untouched)", "tune": "tune half (n 1-100)", "all": "all 200"}[split]
  print(f"{label}: owner JUNK {sum(owner[i] for i in ids)}/{len(ids)}")
  for name, pred in judges.items():
    report(name, pred, ids)
print("v2 misses on owner keeps (would hide):", [r["target"] for r in rows
                                               if not owner[r["item_id"]] and v2[r["item_id"]] >= tau])
