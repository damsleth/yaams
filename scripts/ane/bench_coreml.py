"""Speed + power of the Core ML encoder per compute unit, on real junk rows (2 pairs per row).

  .venv/bin/python bench_coreml.py --model junk.mlpackage --tokenizer CKPT_DIR --rows dev.jsonl \
      [--units CPU_AND_NE,CPU_AND_GPU,ALL,CPU_ONLY] [--seconds 20] [--score-out preds.jsonl --score-rows a.jsonl ...]
"""
import argparse
import json
import math
import os
import sys
import time

import coremltools as ct
import numpy as np
import power
from transformers import AutoTokenizer

sys.path.insert(0, os.environ.get("JEFF_SRC", os.path.expanduser("~/code/jeff/src")))
from jeff.encoder import first_segment  # noqa: E402
from jeff.model import describe, options  # noqa: E402


def pairs(rows):
  firsts, seconds = [], []
  for r in rows:
    keys, descs = options(r["question"])
    assert keys == ["false", "true"], keys
    for k, d in zip(keys, descs, strict=True):
      firsts.append(first_segment(r))
      seconds.append(f"Option {k}: {describe(d)}")
  return firsts, seconds


def encode(tok, rows, buckets):
  """Tokenize pairs and route each to the smallest static length that fits.
  Returns {bucket: (pair_indices, ids, mask)} and the indices of rows too long for any bucket."""
  f, s = pairs(rows)
  enc = tok(f, s)["input_ids"]
  routed, too_long = {b: [] for b in buckets}, set()
  for k, ids in enumerate(enc):
    fit = [b for b in buckets if len(ids) <= b]
    if fit:
      routed[min(fit)].append(k)
    else:
      too_long.add(k // 2)
  out = {}
  for b, idx in routed.items():
    idx = [k for k in idx if k // 2 not in too_long]
    if not idx:
      continue
    ids = np.zeros((len(idx), b), np.int32)
    mask = np.zeros((len(idx), b), np.int32)
    for j, k in enumerate(idx):
      ids[j, :len(enc[k])] = enc[k]
      mask[j, :len(enc[k])] = 1
    out[b] = (idx, ids, mask)
  return out, too_long


def run(models, routed, batch, n_pairs):
  scores = [None] * n_pairs
  for b, (idx, ids, mask) in routed.items():
    for s in range(0, len(ids), batch):
      i, m = ids[s:s + batch], mask[s:s + batch]
      pad = batch - len(i)
      if pad:
        i = np.concatenate([i, np.zeros((pad, b), np.int32)])
        m = np.concatenate([m, np.zeros((pad, b), np.int32)])
      out = np.asarray(models[b].predict({"input_ids": i, "attention_mask": m})["score"]).reshape(-1)
      for j, k in enumerate(idx[s:s + batch]):
        scores[k] = float(out[j])
  return scores


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--model", required=True, help="path prefix; <prefix>-L<bucket>.mlpackage per bucket")
  ap.add_argument("--buckets", default="128,256")
  ap.add_argument("--tokenizer", required=True)
  ap.add_argument("--rows", required=True)
  ap.add_argument("--units", default="CPU_AND_NE,CPU_AND_GPU,ALL,CPU_ONLY")
  ap.add_argument("--seconds", type=float, default=20)
  ap.add_argument("--batch", type=int, default=16)
  ap.add_argument("--temperature", type=float, default=1.0)
  ap.add_argument("--score-rows", nargs="*", default=[])
  ap.add_argument("--score-out")
  a = ap.parse_args()
  buckets = [int(x) for x in a.buckets.split(",")]
  tok = AutoTokenizer.from_pretrained(a.tokenizer)
  rows = [json.loads(line) for line in open(a.rows)]
  routed, too_long = encode(tok, rows, buckets)
  n_rows = len(rows) - len(too_long)
  print(f"{len(rows)} rows ({len(too_long)} too long, skipped); pairs per bucket "
        f"{ {b: len(v[0]) for b, v in routed.items()} }", flush=True)
  base = power.idle(10)
  print(f"idle SoC {base['soc']:.1f} W", flush=True)

  def load(units):
    return {b: ct.models.MLModel(f"{a.model}-L{b}.mlpackage", compute_units=getattr(ct.ComputeUnit, units))
            for b in buckets}

  results = []
  for name in a.units.split(","):
    t = time.perf_counter()
    models = load(name)
    run(models, routed, a.batch, 2 * len(rows))  # warm-up: first call loads/compiles for the device
    load_s = time.perf_counter() - t
    n = 0
    with power.Power() as p:
      t0 = time.perf_counter()
      while time.perf_counter() - t0 < a.seconds:
        run(models, routed, a.batch, 2 * len(rows))
        n += n_rows
      dt = time.perf_counter() - t0
    r = power.report(f"coreml {name}", n, dt, p, base)
    r["load_and_first_pass_s"] = load_s
    results.append(r)
    del models
    time.sleep(3)
  if a.score_out:  # P(junk) per row on the eval sets, with the checkpoint temperature
    models = load("CPU_AND_NE")
    with open(a.score_out, "w") as f:
      for path in a.score_rows:
        rs = [json.loads(line) for line in open(path)]
        r_routed, r_long = encode(tok, rs, buckets)
        s = run(models, r_routed, a.batch, 2 * len(rs))
        for k, r in enumerate(rs):
          if k in r_long:
            continue
          lf, lt = s[2 * k] / a.temperature, s[2 * k + 1] / a.temperature
          f.write(json.dumps({"id": r["id"], "p_junk": 1 / (1 + math.exp(lf - lt))}) + "\n")
    print("scores ->", a.score_out)
  json.dump(results, open(f"{a.model}.bench.json", "w"), indent=1)


if __name__ == "__main__":
  main()
