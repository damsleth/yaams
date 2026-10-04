"""Convert a jeff encoder (ModernBERT/mmBERT) junk checkpoint to Core ML and report where it runs.

Separate venv (coremltools is not a yaams dependency):
  uv venv --python 3.12 .venv-ane && uv pip install --python .venv-ane/bin/python \
    coremltools "transformers==5.17.0" torch safetensors numpy pillow
Then convert per static length bucket (128 and 256 cover the junk band's pairs):
  .venv-ane/bin/python scripts/ane/convert.py --checkpoint CKPT --out junk-mmbert-L128.mlpackage --length 128

  .venv/bin/python convert.py --checkpoint DIR --out model.mlpackage [--batch 16 --length 256]
  .venv/bin/python convert.py --base jhu-clsp/mmBERT-base --out probe.mlpackage   # untrained probe

Input: input_ids/attention_mask int32 [batch, length] (static shapes: the ANE wants them).
Output: score [batch] = scorer(CLS hidden), exactly jeff.encoder's per-pair score.
"""
import argparse
import collections
import time
from pathlib import Path

import coremltools as ct
import numpy as np
import torch
from coremltools.converters.mil import Builder as mb
from coremltools.converters.mil.frontend.torch.ops import _get_inputs
from coremltools.converters.mil.frontend.torch.torch_op_registry import register_torch_op
from safetensors.torch import load_file
from transformers import AutoModel, AutoTokenizer


@register_torch_op(torch_alias=["int"], override=True)
def _int(context, node):
  # coremltools 9 casts a constant with int(x.val), which fails on a 1-element array
  x = _get_inputs(context, node)[0]
  if x.val is not None:
    v = np.asarray(x.val)
    context.add(mb.const(val=np.int32(v.reshape(-1)[0]) if v.size == 1 else v.astype(np.int32), name=node.name))
  else:
    context.add(mb.cast(x=x, dtype="int32", name=node.name))


@register_torch_op
def new_ones(context, node):
  # tensor.new_ones(size, ...): ModernBERT builds its masks with it; coremltools 9 has no converter
  inputs = _get_inputs(context, node)
  shape = inputs[1]
  code = inputs[2].val if len(inputs) > 2 and inputs[2] is not None else None
  # torch ScalarType codes: 11 bool, 3 int32, 4 int64; anything else -> float
  if code == 11:
    value, cast = True, "bool"
  elif code in (3, 4):
    value, cast = 1, "int32"
  else:
    value, cast = 1.0, None
  if shape.val is not None and np.size(shape.val) == 0:  # new_ones(()) -> a scalar
    v = {"bool": np.bool_(True), "int32": np.int32(1)}.get(cast, np.float32(1.0))
    context.add(mb.const(val=v, name=node.name))
    return
  filled = mb.fill(shape=mb.cast(x=shape, dtype="int32"), value=float(value))
  context.add(mb.cast(x=filled, dtype=cast, name=node.name) if cast else mb.identity(x=filled, name=node.name))


class PairScorer(torch.nn.Module):
  """CLS score per pair. Builds ModernBERT's two attention masks itself (additive, from a
  constant distance matrix) and hands them over as the layer-type dict, which skips
  transformers' mask builders: those do not trace/convert cleanly."""

  def __init__(self, backbone, scorer, length):
    super().__init__()
    self.backbone, self.scorer = backbone, scorer
    self.kind = backbone.config.model_type  # modernbert | gemma3_text (bidirectional) | bert
    idx = torch.arange(length)
    # sliding window: ModernBERT's is 64 (half of local_attention=128); Gemma-3's 1024 exceeds our buckets
    half = getattr(backbone.config, "sliding_window", None) or length
    window = ((idx[:, None] - idx[None, :]).abs() <= half).float()
    self.register_buffer("window", window[None, None], persistent=False)  # [1,1,L,L], 1 = may attend

  def forward(self, input_ids, attention_mask):
    if self.kind == "bert":  # BERT's extended mask is plain arithmetic: the stock path converts
      hidden = self.backbone(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
      return self.scorer(hidden[:, 0]).squeeze(-1)
    # float arithmetic only (no bool &): converts cleanly and stays on the ANE
    keys = attention_mask[:, None, None, :].float()  # [B,1,1,L], 1 = real token
    neg = -1e4  # fp16-safe "minus infinity"
    full = (1.0 - keys * torch.ones_like(self.window)) * neg
    sliding = (1.0 - keys * self.window) * neg
    hidden = self.backbone(input_ids=input_ids,
                           attention_mask={"full_attention": full, "sliding_attention": sliding}).last_hidden_state
    return self.scorer(hidden[:, 0]).squeeze(-1)


def parity(model, tokenizer, length, n=8):
  """Max |score - stock forward| on real padded pairs: the wrapper must equal transformers."""
  firsts = ["Question: Is this junk?\n\nState:\n" + "x " * (i * 13 % 90) for i in range(n)]
  seconds = [f"Option {'true' if i % 2 else 'false'}: something" for i in range(n)]
  enc = tokenizer(firsts, seconds, padding="max_length", max_length=length, return_tensors="pt")
  with torch.no_grad():
    ours = model(enc["input_ids"].int(), enc["attention_mask"].int())
    ref = model.scorer(model.backbone(input_ids=enc["input_ids"],
                                      attention_mask=enc["attention_mask"]).last_hidden_state[:, 0]).squeeze(-1)
  return float((ours - ref).abs().max())


def load(args):
  src = args.checkpoint or args.base
  backbone = AutoModel.from_pretrained(src, dtype=torch.float32, attn_implementation="eager")
  scorer = torch.nn.Linear(backbone.config.hidden_size, 1)
  if args.checkpoint:
    scorer.load_state_dict(load_file(str(Path(args.checkpoint) / "scorer.safetensors")))
  tokenizer = AutoTokenizer.from_pretrained(src)
  return PairScorer(backbone, scorer.float(), args.length).eval(), tokenizer


def placement(path, units):
  compiled = str(Path(path).with_suffix(".mlmodelc"))
  if not Path(compiled).exists():  # a temp compile vanishes with its MLModel; keep one next to the package
    ct.utils.compile_model(str(path), compiled)
  plan = ct.models.compute_plan.MLComputePlan.load_from_path(path=compiled, compute_units=units)
  counts, by_type = collections.Counter(), collections.defaultdict(collections.Counter)

  def walk(block):
    for op in block.operations:
      usage = plan.get_compute_device_usage_for_mlprogram_operation(op)
      if usage is not None:
        dev = type(usage.preferred_compute_device).__name__.replace("MLComputeDevice", "").replace("ComputeDevice", "")
        counts[dev] += 1
        by_type[op.operator_name][dev] += 1
      for inner in getattr(op, "blocks", []) or []:
        walk(inner)

  for fn in plan.model_structure.program.functions.values():
    walk(fn.block)
  off = {t: dict(c) for t, c in by_type.items() if any("NeuralEngine" not in d for d in c)}
  return dict(counts), off


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--checkpoint")
  ap.add_argument("--base")
  ap.add_argument("--out", required=True)
  ap.add_argument("--batch", type=int, default=16)
  ap.add_argument("--length", type=int, default=256)
  a = ap.parse_args()
  model, tokenizer = load(a)
  print(f"wrapper vs stock forward, max |diff|: {parity(model, tokenizer, a.length):.2e}")
  ids = torch.ones((a.batch, a.length), dtype=torch.int32)
  mask = torch.ones((a.batch, a.length), dtype=torch.int32)
  t = time.perf_counter()
  with torch.no_grad():
    traced = torch.jit.trace(model, (ids, mask), strict=False)
  mlm = ct.convert(
    traced,
    inputs=[ct.TensorType("input_ids", shape=ids.shape, dtype=np.int32),
            ct.TensorType("attention_mask", shape=mask.shape, dtype=np.int32)],
    outputs=[ct.TensorType("score", dtype=np.float32)],
    compute_precision=ct.precision.FLOAT16,
    minimum_deployment_target=ct.target.macOS15,
  )
  mlm.save(a.out)
  print(f"converted in {time.perf_counter() - t:.0f}s -> {a.out}")
  for name, units in (("CPU_AND_NE", ct.ComputeUnit.CPU_AND_NE), ("CPU_AND_GPU", ct.ComputeUnit.CPU_AND_GPU)):
    print(f"op placement with {name}: {placement(a.out, units)}")


if __name__ == "__main__":
  main()
