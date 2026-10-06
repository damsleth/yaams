"""Speed + power of Jeff (Qwen3.5 0.8B) on MLX, same rows and power method as bench_coreml.py.

  ~/code/jeff/.venv/bin/python scripts/ane/bench_jeff_mlx.py --checkpoint CKPT --rows dev.jsonl [--batch 8]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import power  # noqa: E402
from jeff.mlx_backend import MlxDecisionModel  # noqa: E402


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--checkpoint", required=True)
  ap.add_argument("--rows", required=True)
  ap.add_argument("--batch", type=int, default=8)
  ap.add_argument("--seconds", type=float, default=20)
  a = ap.parse_args()
  rows = [json.loads(line) for line in open(a.rows)]
  inputs = [{"state": r["state"], "question": r["question"]} for r in rows]
  base = power.idle(10)
  print(f"idle SoC {base['soc']:.1f} W", flush=True)
  t = time.perf_counter()
  m = MlxDecisionModel(a.checkpoint)
  m.batch_size = a.batch
  m.decide(inputs[:a.batch])
  load_s = time.perf_counter() - t
  n = 0
  with power.Power() as p:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < a.seconds:
      m.decide(inputs)
      n += len(inputs)
    dt = time.perf_counter() - t0
  r = power.report(f"jeff mlx gpu (batch {a.batch})", n, dt, p, base)
  r["load_and_first_pass_s"] = load_s
  json.dump([r], open("jeff-mlx.bench.json", "w"), indent=1)


if __name__ == "__main__":
  main()
