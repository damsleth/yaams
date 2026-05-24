from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import click

from yaams.enrich import Embedder, EntityTagger
from yaams.ingest import Item
from yaams.schema import DEFAULT_EMBEDDING_DIM

# Where HF model weights live by default. We keep them out of `~/.cache`
# because they're durable, multi-GB artifacts, not regenerable cache.
DEFAULT_MODELS_DIR = "~/.local/share/huggingface"

_CONFIG_HELP = (
  "Path to config.yaml. Auto-resolves from $YAAMS_CONFIG, "
  "~/.config/yaams/config.yaml, or repo root if omitted."
)


def config_option(f):
  return click.option("--config", "config_path", default=None, help=_CONFIG_HELP)(f)


def _embed_config(cfg: dict) -> dict:
  raw = dict(cfg.get("embed", {}))
  model = raw.pop("model")
  # Config wins; otherwise respect an externally set $HF_HOME; otherwise fall
  # back to DEFAULT_MODELS_DIR so models survive `~/.cache` wipes.
  if "models_dir" not in raw and not os.environ.get("HF_HOME"):
    raw["models_dir"] = DEFAULT_MODELS_DIR
  return {"model": model, **raw}


def _embedding_dim(cfg: dict) -> int:
  return int(cfg.get("embed", {}).get("dimension", DEFAULT_EMBEDDING_DIM))


def _entities_config(cfg: dict) -> dict:
  return dict(cfg.get("entities", {}))


def _entity_dictionary(cfg: dict) -> list[dict]:
  return list(_entities_config(cfg).get("dictionary", []))


def _progress(iterable: Iterable[Item], desc: str, unit: str = "it") -> Iterable[Item]:
  try:
    from tqdm import tqdm

    return tqdm(iterable, desc=desc, unit=unit)
  except ImportError:
    return iterable


def _date(value: str | None) -> str:
  if not value:
    return "n/a"
  return value[:10]


def _size_mb(path: Path) -> float:
  return path.stat().st_size / (1024 * 1024)


def _format_duration(ms: float) -> str:
  if ms < 1000:
    return f"{ms:.0f}ms"
  seconds = ms / 1000
  if seconds < 60:
    return f"{seconds:.1f}s"
  minutes, seconds = divmod(seconds, 60)
  return f"{int(minutes)}m{seconds:04.1f}s"


def _format_throughput(seen: int, ms: float) -> str:
  if ms <= 0 or seen <= 0:
    return ""
  rate = seen / (ms / 1000)
  return f", {rate:,.1f} items/s"


@dataclass
class ProcessingContext:
  cfg: dict
  _embedder: Embedder | None = field(default=None, init=False)
  _tagger: EntityTagger | None = field(default=None, init=False)

  @property
  def embedder(self) -> Embedder:
    if self._embedder is None:
      self._embedder = Embedder(**_embed_config(self.cfg))
    return self._embedder

  @property
  def tagger(self) -> EntityTagger:
    if self._tagger is None:
      ent_cfg = _entities_config(self.cfg)
      self._tagger = EntityTagger(
        ent_cfg.get("spacy_model"),
        _entity_dictionary(self.cfg),
        spacy_model_nb=ent_cfg.get("spacy_model_nb"),
      )
    return self._tagger
