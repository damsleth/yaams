"""Compress the Core ML junk encoder: int8 weights, 8-bit palettization, W8A8.

  .venv/bin/python quantize.py --model junk-mmbert --variant int8|pal8|w8a8 [--buckets 128,256]

Writes <model>-<variant>-L<bucket>.mlpackage and prints op placement on CPU_AND_NE.
W8A8 calibrates activations on 64 real dev pairs per bucket.
"""
import argparse
import json
import os

import bench_coreml as b
import coremltools as ct
import coremltools.optimize.coreml as cto
from convert import placement
from transformers import AutoTokenizer

CK = os.path.expanduser(os.environ.get("JUNK_CKPT", "~/code/jeff/checkpoints/yaams-junk-mmbert"))
DEV = os.path.expanduser("~/brain/feed/eval/jeff/ft/junk-v2/dev.jsonl")


def calibration(bucket, n=64, batch=16):
  tok = AutoTokenizer.from_pretrained(CK)
  rows = [json.loads(line) for line in open(DEV)][:400]
  routed, _ = b.encode(tok, rows, [bucket])
  _, ids, mask = routed[bucket]
  return [{"input_ids": ids[s:s + batch], "attention_mask": mask[s:s + batch]}
          for s in range(0, min(n, len(ids) - batch + 1), batch)]


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--model", default="junk-mmbert")
  ap.add_argument("--variant", required=True, choices=["int8", "pal8", "w8a8"])
  ap.add_argument("--skip-gather", action="store_true", help="leave the embedding table in fp16")
  ap.add_argument("--suffix", default="")
  ap.add_argument("--linear-only", action="store_true")
  ap.add_argument("--granularity", default="per_channel", choices=["per_channel", "per_tensor"])
  ap.add_argument("--pal-mode", default="uniform", choices=["uniform", "kmeans"])
  ap.add_argument("--buckets", default="128,256")
  a = ap.parse_args()
  for bucket in (int(x) for x in a.buckets.split(",")):
    m = ct.models.MLModel(f"{a.model}-L{bucket}.mlpackage", compute_units=ct.ComputeUnit.CPU_ONLY)
    skip = {"gather": None} if a.skip_gather else {}
    q8 = cto.OpLinearQuantizerConfig(mode="linear_symmetric", dtype="int8", granularity=a.granularity)
    if a.linear_only:  # quantize only the linear layers' weights; every other constant stays fp16
      wq = cto.OptimizationConfig(global_config=None, op_type_configs={"linear": q8})
    else:
      wq = cto.OptimizationConfig(global_config=q8, op_type_configs=skip)
    if a.variant == "int8":
      out = cto.linear_quantize_weights(m, config=wq)
    elif a.variant == "pal8":
      out = cto.palettize_weights(m, config=cto.OptimizationConfig(
        global_config=cto.OpPalettizerConfig(nbits=8, mode=a.pal_mode, granularity="per_tensor"),
        op_type_configs=skip))
    else:
      # activations of the linear layers only: a global config also hits the int32 token inputs
      aq = cto.OptimizationConfig(global_config=None, op_type_configs={
        "linear": cto.experimental.OpActivationLinearQuantizerConfig(mode="linear_symmetric")})
      act = cto.experimental.linear_quantize_activations(m, aq, calibration(bucket))
      out = cto.linear_quantize_weights(act, config=wq)
    path = f"{a.model}-{a.variant}{a.suffix}-L{bucket}.mlpackage"
    out.save(path)
    counts, _ = placement(path, ct.ComputeUnit.CPU_AND_NE)
    print(f"{path}: placement CPU_AND_NE {counts}", flush=True)


if __name__ == "__main__":
  main()
