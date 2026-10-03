"""One ModernBERT-sized encoder layer, two layouts, on the ANE: is Apple's (B,C,1,S) layout worth a rewrite?

(a) "current": Linear on (B, S, C) -- what the naive conversion runs today (effectively channels-last)
(b) "ane": ml-ane-transformers style: (B, C, 1, S), 1x1 Conv2d projections, per-head split attention
    via einsum, LayerNorm over the channel axis.
Same dims as mmBERT-base: hidden 768, 12 heads, GeGLU intermediate 1152, batch 16, seq 128. Random
weights, no RoPE (identical cost in both). Reports placement and mean latency per call on CPU_AND_NE.
"""
import collections
import time

import coremltools as ct
import numpy as np
import torch
from torch import nn

B, S, C, H, I = 16, 128, 768, 12, 1152
D = C // H


class Current(nn.Module):
  def __init__(self):
    super().__init__()
    self.ln1, self.ln2 = nn.LayerNorm(C), nn.LayerNorm(C)
    self.qkv, self.o = nn.Linear(C, 3 * C, bias=False), nn.Linear(C, C, bias=False)
    self.wi, self.wo = nn.Linear(C, 2 * I, bias=False), nn.Linear(I, C, bias=False)

  def forward(self, x, mask):  # x [B,S,C], mask [B,1,1,S] additive
    q, k, v = self.qkv(self.ln1(x)).view(B, S, 3, H, D).unbind(2)
    q, k, v = (t.transpose(1, 2) for t in (q, k, v))  # [B,H,S,D]
    w = torch.softmax(q @ k.transpose(-1, -2) * D ** -0.5 + mask, dim=-1)
    x = x + self.o((w @ v).transpose(1, 2).reshape(B, S, C))
    a, g = self.wi(self.ln2(x)).chunk(2, dim=-1)
    return x + self.wo(nn.functional.gelu(a) * g)


class LayerNormANE(nn.Module):  # normalize over channels of (B, C, 1, S)
  def __init__(self, c, eps=1e-5):
    super().__init__()
    self.w, self.b, self.eps = nn.Parameter(torch.ones(c)), nn.Parameter(torch.zeros(c)), eps

  def forward(self, x):
    mu = x.mean(1, keepdim=True)
    z = x - mu
    var = (z * z).mean(1, keepdim=True)
    return z * torch.rsqrt(var + self.eps) * self.w.view(1, -1, 1, 1) + self.b.view(1, -1, 1, 1)


class Ane(nn.Module):
  def __init__(self):
    super().__init__()
    self.ln1, self.ln2 = LayerNormANE(C), LayerNormANE(C)
    self.q, self.k, self.v = (nn.Conv2d(C, C, 1, bias=False) for _ in range(3))
    self.o = nn.Conv2d(C, C, 1, bias=False)
    self.wi, self.wo = nn.Conv2d(C, 2 * I, 1, bias=False), nn.Conv2d(I, C, 1, bias=False)

  def forward(self, x, mask):  # x [B,C,1,S], mask [B,S,1,1]-style additive over keys: [B,S_k,1,1]
    h = self.ln1(x)
    q, k, v = self.q(h), self.k(h), self.v(h)
    outs = []
    for qh, kh, vh in zip(q.split(D, 1), k.split(D, 1), v.split(D, 1), strict=True):
      w = torch.einsum("bchq,bchk->bkhq", qh, kh) * D ** -0.5 + mask  # [B, S_k, 1, S_q]
      w = torch.softmax(w, dim=1)
      outs.append(torch.einsum("bkhq,bchk->bchq", w, vh))
    x = x + self.o(torch.cat(outs, 1))
    a, g = self.wi(self.ln2(x)).chunk(2, dim=1)
    return x + self.wo(nn.functional.gelu(a) * g)


def build(name, model, x, mask):
  with torch.no_grad():
    tr = torch.jit.trace(model.eval(), (x, mask))
  m = ct.convert(tr, inputs=[ct.TensorType("x", shape=x.shape), ct.TensorType("mask", shape=mask.shape)],
                 outputs=[ct.TensorType("y")], compute_precision=ct.precision.FLOAT16,
                 minimum_deployment_target=ct.target.macOS15)
  path = f"layout-{name}.mlpackage"
  m.save(path)
  return path


def placement(path):
  compiled = path.replace(".mlpackage", ".mlmodelc")
  ct.utils.compile_model(path, compiled)
  plan = ct.models.compute_plan.MLComputePlan.load_from_path(path=compiled, compute_units=ct.ComputeUnit.CPU_AND_NE)
  c = collections.Counter()
  for fn in plan.model_structure.program.functions.values():
    for op in fn.block.operations:
      u = plan.get_compute_device_usage_for_mlprogram_operation(op)
      if u is not None:
        c[type(u.preferred_compute_device).__name__.replace("MLComputeDevice", "")] += 1
  return dict(c)


def bench(path, feed, n=300):
  m = ct.models.MLModel(path, compute_units=ct.ComputeUnit.CPU_AND_NE)
  for _ in range(20):
    m.predict(feed)
  t = time.perf_counter()
  for _ in range(n):
    m.predict(feed)
  return (time.perf_counter() - t) / n * 1000


def main():
  torch.manual_seed(0)
  cur, ane = Current(), Ane()
  x_cur = torch.randn(B, S, C)
  m_cur = torch.zeros(B, 1, 1, S)
  x_ane = x_cur.transpose(1, 2).unsqueeze(2).contiguous()  # [B,C,1,S]
  m_ane = torch.zeros(B, S, 1, 1)
  for name, model, x, m in (("current", cur, x_cur, m_cur), ("ane", ane, x_ane, m_ane)):
    path = build(name, model, x, m)
    ms = bench(path, {"x": x.numpy().astype(np.float32), "mask": m.numpy().astype(np.float32)})
    print(f"{name:8s} {ms:7.2f} ms/call (batch {B} x seq {S}) placement {placement(path)}", flush=True)


if __name__ == "__main__":
  main()
