from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence


class Embedder:
  def __init__(
    self,
    model: str,
    device: str | None = None,
    batch_size: int = 32,
    dimension: int | None = None,
    offline: bool = True,
    models_dir: str | Path | None = None,
    quiet: bool = False,
  ):
    # Set HF_HOME before importing sentence_transformers: huggingface_hub
    # freezes cache paths into module constants at import time.
    if models_dir is not None:
      os.environ["HF_HOME"] = str(Path(models_dir).expanduser())

    if quiet:
      # Same reason as HF_HOME — these env vars are read at import time by
      # huggingface_hub / transformers / tqdm, so set them before the import.
      os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
      os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
      os.environ.setdefault("TQDM_DISABLE", "1")

    try:
      from sentence_transformers import SentenceTransformer
    except ImportError as exc:
      raise RuntimeError(
        "sentence-transformers is required for embedding. Install requirements.txt."
      ) from exc

    kwargs = {}
    if device:
      kwargs["device"] = device
    # Use local_files_only rather than HF_HUB_OFFLINE env vars: huggingface_hub
    # freezes the offline flag into a module constant at import time, so toggling
    # the env var after import is a no-op.
    try:
      self.model = SentenceTransformer(model, local_files_only=offline, **kwargs)
    except OSError:
      if not offline or not _confirm_download(model):
        raise
      self.model = SentenceTransformer(model, local_files_only=False, **kwargs)
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

