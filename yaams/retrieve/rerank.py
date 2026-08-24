"""Optional cross-encoder reranker for the `--rerank` retrieval path.

Ported from cognitive-ledger's `ledger/rerank.py`. The hybrid path runs first
to produce a candidate pool; the cross-encoder then scores each (query, doc)
pair jointly and the pool is re-sorted by that score. Cross-encoders beat
bi-encoders on top-1 precision; the cost is per-query latency (≈50-200ms for a
~50-candidate pool on CPU), so it's opt-in and the fast path never imports this
module.
"""
from __future__ import annotations

from typing import Any

_RERANKER_CACHE: dict[str, Any] = {}


def get_reranker(model_name: str, max_length: int = 512, device: str | None = None) -> Any:
  """Load and cache a CrossEncoder. First call pays the model load cost.

  ``device`` is passed through to sentence-transformers. Default None lets it
  auto-pick, but the config default is ``cpu``: these cross-encoders are
  unstable on Apple ``mps`` (crash mid-predict) and a ≤100-candidate pool is
  fast enough on CPU. CUDA users can set ``retrieve.rerank.device: cuda``.
  """
  cache_key = f"{model_name}::{max_length}::{device}"
  cached = _RERANKER_CACHE.get(cache_key)
  if cached is not None:
    return cached

  try:
    from sentence_transformers import CrossEncoder  # type: ignore
  except ImportError as exc:  # pragma: no cover - sentence-transformers is a core dep
    raise RuntimeError(
      "--rerank requires sentence-transformers (a core dependency). "
      "Reinstall with: pip install -e ."
    ) from exc

  model = CrossEncoder(model_name, max_length=max_length, device=device)
  _RERANKER_CACHE[cache_key] = model
  return model


def candidate_text(title: str, body: str, max_chars: int = 2048) -> str:
  """Build the candidate text passed to the cross-encoder.

  Pre-truncate by characters to bound transmit size; the tokenizer still does
  final truncation to ``max_length``.
  """
  title = (title or "").strip()
  body = (body or "").strip()
  text = f"{title}\n{body}" if title else body
  if max_chars and len(text) > max_chars:
    text = text[:max_chars]
  return text


def rerank_pairs(
  query: str,
  pairs: list[tuple[str, str]],
  model_name: str,
  batch_size: int = 32,
  max_length: int = 512,
  device: str | None = None,
) -> list[float]:
  """Score each (query, doc) pair. Returns scores in input order."""
  del query  # unused; pairs already contain the query as their first element
  if not pairs:
    return []
  model = get_reranker(model_name, max_length=max_length, device=device)
  scores = model.predict(
    pairs,
    batch_size=batch_size,
    show_progress_bar=False,
    convert_to_numpy=True,
  )
  return [float(s) for s in scores]
