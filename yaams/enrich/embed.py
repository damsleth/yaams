from __future__ import annotations

from typing import Sequence


class Embedder:
  def __init__(
    self,
    model: str,
    device: str | None = None,
    batch_size: int = 32,
    dimension: int | None = None,
  ):
    try:
      from sentence_transformers import SentenceTransformer
    except ImportError as exc:
      raise RuntimeError(
        "sentence-transformers is required for embedding. Install requirements.txt."
      ) from exc

    kwargs = {}
    if device:
      kwargs["device"] = device
    self.model = SentenceTransformer(model, **kwargs)
    self.batch_size = batch_size
    self.dim = self.model.get_sentence_embedding_dimension()
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

