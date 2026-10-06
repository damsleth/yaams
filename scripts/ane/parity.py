"""Parity of Core ML junk scores vs the PyTorch reference on the owner 200 rows.
  .venv/bin/python parity.py scores.jsonl [--tau 0.65]
"""
import argparse
import json
from pathlib import Path

REF = Path.home() / "brain/feed/eval/jeff/ft/junk-mmbert/results"

ap = argparse.ArgumentParser()
ap.add_argument("scores")
ap.add_argument("--tau", type=float, default=0.65)
a = ap.parse_args()
ref = {}
for s in ("sample_tune", "sample_test"):
  for r in map(json.loads, open(REF / f"junk-mmbert-{s}.predictions.jsonl")):
    ref[r["id"]] = r["probabilities"][1]
got = {r["id"]: r["p_junk"] for r in map(json.loads, open(a.scores))}
d = sorted(abs(got[i] - ref[i]) for i in got if i in ref)
flips = sum((got[i] >= a.tau) != (ref[i] >= a.tau) for i in got if i in ref)
print(f"parity n={len(d)} mean={sum(d) / len(d):.4f} max={d[-1]:.4f} flips@{a.tau}={flips}")
