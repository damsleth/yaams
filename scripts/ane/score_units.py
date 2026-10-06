"""Score the owner 200 rows with a bucketed Core ML model on a chosen compute unit.
  .venv/bin/python score_units.py --model PREFIX --units CPU_ONLY --out scores.jsonl
"""
import argparse
import json
import math
import os

import bench_coreml as b
import coremltools as ct
from transformers import AutoTokenizer

CK = os.path.expanduser(os.environ.get("JUNK_CKPT", "~/code/jeff/checkpoints/yaams-junk-mmbert"))
D = os.path.expanduser("~/brain/feed/eval/jeff/ft/junk-v2")
T = 1.305078269931672

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--units", default="CPU_ONLY")
ap.add_argument("--buckets", default="128,256")
ap.add_argument("--out", required=True)
a = ap.parse_args()
buckets = [int(x) for x in a.buckets.split(",")]
tok = AutoTokenizer.from_pretrained(CK)
models = {k: ct.models.MLModel(f"{a.model}-L{k}.mlpackage", compute_units=getattr(ct.ComputeUnit, a.units))
          for k in buckets}
with open(a.out, "w") as f:
  for name in ("sample_tune", "sample_test"):
    rs = [json.loads(line) for line in open(f"{D}/{name}.jsonl")]
    routed, long_ = b.encode(tok, rs, buckets)
    s = b.run(models, routed, 16, 2 * len(rs))
    for k, r in enumerate(rs):
      if k not in long_:
        f.write(json.dumps({"id": r["id"], "p_junk": 1 / (1 + math.exp(s[2 * k] / T - s[2 * k + 1] / T)),
                            "raw": [s[2 * k], s[2 * k + 1]]}) + "\n")
