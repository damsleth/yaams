# Embedding Precision (fp16) and the MLX Question

YAAMS embeds with `BAAI/bge-m3` via `sentence-transformers`. This note records
how embedding precision is handled and why MLX-format conversions of the model
are *not* used.

## What the code does

`Embedder` (`yaams/enrich/embed.py`) casts the model to **fp16 on GPU backends**
(`mps`, `cuda`) and leaves it at **fp32 on CPU**:

```python
self.model.max_seq_length = 512
# fp16 halves the weights on device and runs ~10% faster on GPU backends;
# drift from fp32 is ~1e-4 cosine — negligible for normalized retrieval.
# CPU fp16 ops are poorly supported, so cast only on mps/cuda.
if device and device.split(":")[0] in ("mps", "cuda"):
  self.model = self.model.half()
```

CPU stays fp32 on purpose: half-precision ops are poorly supported on CPU and
would *slow* embedding down, not speed it up. The cast keys off `embed.device`
from config (`mps` by default).

## Why fp16

Measured on this dev machine (Apple Silicon / MPS, `sentence-transformers`
5.5.1, `torch` 2.12.0), 400 mixed-length texts, yaams encode settings (batch 32,
`max_seq_length` 512, normalized), median of 3 timed runs after warmup:

| Variant            | Throughput      | Weights on device | Quality vs fp32           |
| ------------------ | --------------- | ----------------- | ------------------------- |
| bge-m3 **fp32**    | 63.6 texts/s    | ~2.3 GB           | reference                 |
| bge-m3 **fp16**    | 69.8 texts/s    | ~1.14 GB          | cosine min 0.99987 / mean 1.0002 |

fp16 is ~10% faster and halves resident model memory; the per-text cosine drift
from fp32 lands in the fourth decimal, far below anything hybrid retrieval cares
about.

## What fp16 does *not* change

- **On-disk DB size is unchanged.** Vectors are still stored as **float32**.
  `_embedding_to_blob` (`store.py`, `retrieve/hybrid.py`) upcasts with
  `embedding.astype("float32").tobytes()` before the blob hits sqlite-vec's
  `FLOAT[1024]` column. The savings are device memory + compute, not storage.
- **No re-embed needed.** Rows embedded as fp32 before this change and rows
  embedded as fp16 after it coexist in the same index — both are stored as
  float32, and the ~1e-4 drift is irrelevant to ranking. A re-embed is only
  required when the *dimension* or *model* changes (see
  [schema-migrations.md](schema-migrations.md#changing-embedding-models)).

## Why not MLX-converted models

Models like `mlx-community/bge-m3-mlx-fp16` are the same bge-m3 weights cast to
fp16 and re-laid-out for Apple's MLX runtime. Evaluated and declined for
embedding:

- **The runtime can't be loaded without surgery.** `Embedder` is wired to
  `SentenceTransformer` (PyTorch). MLX safetensors won't load there. A backend
  branch plus `mlx-embeddings` would be required — and `mlx-embeddings` pulls
  `mlx-vlm`, `opencv-python`, `pandas`, `datasets`, `fastapi`, and forces a
  `numpy` major-version bump that conflicts with the working torch /
  sentence-transformers pins.
- **The measurable win is fp16, and we already have it.** The two things an
  MLX-fp16 conversion changes are precision (fp32→fp16, captured above with one
  line and no new deps) and the runtime (PyTorch-MPS→MLX). MLX's large speedups
  are in *autoregressive LLM decode*, not single-pass encoder embedding, where
  PyTorch's MPS backend is already efficient.
- **Embedding is not the bottleneck.** Ingest is dominated by source fetch and
  storage, not the embed step.
- **Provenance.** The conversions are community uploads (dense-only — no
  sparse/ColBERT) on a young library, versus the well-exercised
  sentence-transformers path.

If a *generative* local LLM backend is ever added for the parser/synthesis
(`synth.backend`), MLX is worth revisiting **there** — decode is its strong
suit. For embeddings, it is not worth the integration cost.
