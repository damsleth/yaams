from __future__ import annotations

import os
import sys
from typing import Sequence


class Embedder:
  def __init__(
    self,
    model: str,
    device: str | None = None,
    batch_size: int = 32,
    dimension: int | None = None,
    offline: bool = True,
  ):
    if offline:
      os.environ["HF_HUB_OFFLINE"] = "1"
      os.environ["TRANSFORMERS_OFFLINE"] = "1"

    try:
      from sentence_transformers import SentenceTransformer
    except ImportError as exc:
      raise RuntimeError(
        "sentence-transformers is required for embedding. Install requirements.txt."
      ) from exc

    kwargs = {}
    if device:
      kwargs["device"] = device
    try:
      self.model = SentenceTransformer(model, **kwargs)
    except OSError:
      if not offline or not _confirm_download(model):
        raise
      os.environ["HF_HUB_OFFLINE"] = "0"
      os.environ["TRANSFORMERS_OFFLINE"] = "0"
      self.model = SentenceTransformer(model, **kwargs)
      os.environ["HF_HUB_OFFLINE"] = "1"
      os.environ["TRANSFORMERS_OFFLINE"] = "1"
    self.model.max_seq_length = 512
    self.batch_size = batch_size
    self.dim = self.model.get_embedding_dimension()
    if dimension is not None and self.dim != int(dimension):
      raise ValueError(
        f"Embedding model dimension {self.dim} does not match configured {dimension}"
      )

  def embed_batch(self, texts: Sequence[str]):
    return self.model.encode(
      list(texts),
      batch_size=self.batch_size,
      show_progress_bar=False,
      convert_to_numpy=True,
      normalize_embeddings=True,
    )


def _confirm_download(model: str) -> bool:
  if not sys.stdin.isatty():
    return False
  prompt = f"Model '{model}' not found in local HF cache. Download from huggingface.co? [y/N] "
  try:
    answer = input(prompt).strip().lower()
  except EOFError:
    return False
  return answer in ("y", "yes")

