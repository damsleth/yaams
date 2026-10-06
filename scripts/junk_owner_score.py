"""Score every junk judge (Sonnet runs, Jev, zero-shot and fine-tuned Jeff)
against the owner's 200 blind labels (jeff/ft/junk-v1/sample_sheet.tsv)."""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from jev_junk_score import kappa  # noqa: E402
from junk_sonnet_pass import load_ckpt  # noqa: E402

E = Path.home() / "brain/feed/eval"
rows = list(csv.DictReader(open(f"{E}/jeff/ft/junk-v1/sample_sheet.tsv"), delimiter="\t"))
owner = {r["item_id"]: r["owner_verdict (J/K)"] == "J" for r in rows}
r1, r2 = load_ckpt(1), load_ckpt(2)
load = lambda p: {r["item_id"]: r["noul"] for r in map(json.loads, open(p))}  # noqa: E731
jev_en, jev_nb = load(f"{E}/jev/junk-en.jsonl"), load(f"{E}/jev/junk-nb.jsonl")
jeff_en = load(f"{E}/jeff/junk-en.jsonl")
ft = {}
for p in map(json.loads, open(f"{E}/jeff/ft/junk-v1/results/junk-v1-sample_unlabeled.predictions.jsonl")):
  ft[p["id"].split(":", 1)[1]] = p["probabilities"][1]
lang = {r["item_id"]: r["lang"] for r in map(json.loads, open(f"{E}/jev/junk-nb.jsonl"))}

judges = {
  "Sonnet run1": {i: r1[i] == "JUNK" for i in owner},
  "Sonnet run2": {i: r2[i] == "JUNK" for i in owner},
  "Sonnet consensus-JUNK (both)": {i: r1[i] == r2[i] == "JUNK" for i in owner},
  "Jev en @0.5": {i: jev_en[i] >= 0.5 for i in owner},
  "Jev nb @0.5": {i: jev_nb[i] >= 0.5 for i in owner},
  "Jeff zero-shot en @0.5": {i: jeff_en[i] >= 0.5 for i in owner},
  "junk-v1 training label (any JUNK)": {i: "JUNK" in (r1[i], r2[i]) or jev_nb[i] >= 0.5 for i in owner},
  "Jeff fine-tuned v1 @0.5": {i: ft[i] >= 0.5 for i in owner},
  "all-JUNK baseline": {i: True for i in owner},
}
n = len(owner)
print(f"{n} rows, owner JUNK {sum(owner.values())}, KEEP {n - sum(owner.values())}")
print(f"{'judge':36s} acc   kappa  JUNK-recall  KEEP-recall  KEEP-precision  acc nb/en")
for name, pred in judges.items():
  acc = sum(pred[i] == owner[i] for i in owner) / n
  k = kappa((pred[i], owner[i]) for i in owner)
  jr = sum(pred[i] for i in owner if owner[i]) / sum(owner.values())
  kr = sum(not pred[i] for i in owner if not owner[i]) / (n - sum(owner.values()))
  kk = [i for i in owner if not pred[i]]
  kp = sum(not owner[i] for i in kk) / len(kk) if kk else float("nan")
  by = {lg: [i for i in owner if lang.get(i) == lg] for lg in ("nb", "en")}
  la = "/".join(f"{sum(pred[i] == owner[i] for i in v) / len(v):.2f}" for v in by.values())
  print(f"{name:36s} {acc:.3f} {k:6.3f}  {jr:.3f}        {kr:.3f}        {kp:.3f}          {la}")
print("lang n:", {lg: sum(lang.get(i) == lg for i in owner) for lg in ("nb", "en")})
best = max((kappa((ft[i] >= t / 100, owner[i]) for i in owner), t / 100) for t in range(5, 96))
print(f"fine-tuned best tau {best[1]:.2f} -> kappa {best[0]:.3f} (reported only)")
keeps = [r for r in rows if r["owner_verdict (J/K)"] == "K"]
print("fine-tuned misses on owner KEEP rows (predicted JUNK):")
for r in keeps:
  if ft[r["item_id"]] >= 0.5:
    print(f"  {r['n']}: {r['target']!r}  p_junk={ft[r['item_id']]:.2f}")
