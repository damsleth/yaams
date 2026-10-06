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
scores = {"Jeff v2": v2}
M = E / "jeff/ft/junk-mmbert"
mm_files = [M / f"results/junk-mmbert-{s}.predictions.jsonl" for s in ("sample_tune", "sample_test")]
if all(f.exists() for f in mm_files):  # mmBERT encoder fine-tune (PyTorch eval on KWIN)
  mm = {k: v for f in mm_files for k, v in preds(f).items()}
  scores["mmBERT"] = mm
  mt = max((kappa((mm[i] >= t / 100, owner[i]) for i in tune), t / 100) for t in range(5, 96))[1]
  judges[f"mmBERT fine-tuned @{mt:.2f} (tau from tune)"] = {i: mm[i] >= mt for i in owner}
  ane_f = M / "coreml-ane-scores.jsonl"
  if ane_f.exists():  # the same model converted to Core ML, run on the Neural Engine
    ane = {r["id"].split(":", 1)[1]: r["p_junk"] for r in map(json.loads, open(ane_f))}
    scores["mmBERT Core ML ANE"] = ane
    judges[f"mmBERT Core ML ANE @{mt:.2f} (same tau)"] = {i: ane.get(i, 0) >= mt for i in owner}
    d = sorted(abs(ane[i] - mm[i]) for i in ane if i in mm)
    print(f"ANE vs PyTorch: mean |diff| {sum(d) / len(d):.4f}, max {d[-1]:.4f}, "
          f"flips @{mt:.2f}: {sum((ane[i] >= mt) != (mm[i] >= mt) for i in ane if i in mm)}")
G = E / "gliner2"
for v in ("target", "block"):  # zero-shot GLiNER2 (gliner2-base-v1, MLX via Gliner2Swift)
  f = G / f"out_{v}.jsonl"
  if f.exists():
    g = {r["id"].split(":", 1)[1]: r["p_junk"] for r in map(json.loads, open(f)) if r["p_junk"] is not None}
    scores[f"GLiNER2 {v}"] = g
    gt = max((kappa((g[i] >= t / 100, owner[i]) for i in tune if i in g), t / 100) for t in range(5, 96))[1]
    judges[f"GLiNER2 zero-shot, {v} @{gt:.2f} (tau from tune)"] = {i: g.get(i, 0) >= gt for i in owner}
    ms = sorted(r["ms"] for r in map(json.loads, open(f)))
    print(f"GLiNER2 {v}: median {ms[len(ms) // 2]:.1f} ms/decision, p95 {ms[int(len(ms) * .95)]:.1f}")


def auc(s, ids):
  pos = [s[i] for i in ids if owner[i] and i in s]
  neg = [s[i] for i in ids if not owner[i] and i in s]
  return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
for split in ("test", "tune", "all"):
  ids = [i for i in owner if split == "all" or half[i] == split]
  label = {"test": "TEST half (n 101-200, untouched)", "tune": "tune half (n 1-100)", "all": "all 200"}[split]
  print(f"{label}: owner JUNK {sum(owner[i] for i in ids)}/{len(ids)}")
  for name, pred in judges.items():
    report(name, pred, ids)
  print("  AUC (threshold-free): " + ", ".join(f"{k} {auc(s, ids):.3f}" for k, s in scores.items()))
print("v2 misses on owner keeps (would hide):", [r["target"] for r in rows
                                               if not owner[r["item_id"]] and v2[r["item_id"]] >= tau])
