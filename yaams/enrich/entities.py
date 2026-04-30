from __future__ import annotations

import re
from typing import Iterable

from yaams.store import EntityTag


LABEL_TYPES = {
  "PERSON": "person",
  "ORG": "org",
  "GPE": "place",
  "LOC": "place",
}


class EntityTagger:
  def __init__(
    self,
    spacy_model: str | None,
    dictionary: Iterable[dict] | None = None,
  ):
    self.dictionary_entries = list(dictionary or [])
    self.dictionary = self._build_alias_index(self.dictionary_entries)
    self.alias_pattern = self._build_alias_pattern(self.dictionary)
    self.nlp = self._load_spacy(spacy_model) if spacy_model else None

  def tag(self, content: str) -> list[EntityTag]:
    results: list[EntityTag] = []
    results.extend(self._tag_dictionary(content))
    if self.nlp is not None:
      results.extend(self._tag_ner(content))
    return self._dedupe(results)

  def _tag_dictionary(self, content: str) -> list[EntityTag]:
    if self.alias_pattern is None:
      return []
    tags: list[EntityTag] = []
    for match in self.alias_pattern.finditer(content):
      canonical, entity_type = self.dictionary[match.group(0).casefold()]
      tags.append((canonical, entity_type, 1.0, "dictionary"))
    return tags

  def _tag_ner(self, content: str) -> list[EntityTag]:
    doc = self.nlp(content[:5000])
    tags: list[EntityTag] = []
    for ent in doc.ents:
      entity_type = LABEL_TYPES.get(ent.label_)
      if entity_type is None:
        continue
      canonical, resolved_type = self._resolve_dictionary(ent.text)
      if canonical:
        tags.append((canonical, resolved_type, 1.0, "dictionary"))
      else:
        tags.append(
          (normalize_ner_canonical(ent.text, entity_type), entity_type, 0.7, "ner")
        )
    return tags

  def _resolve_dictionary(self, value: str) -> tuple[str | None, str | None]:
    match = self.dictionary.get(value.strip().casefold())
    if match is None:
      return None, None
    return match

  def _build_alias_index(
    self,
    dictionary: Iterable[dict],
  ) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for entry in dictionary:
      canonical = str(entry["canonical"])
      entity_type = str(entry.get("type", "other"))
      index[canonical.casefold()] = (canonical, entity_type)
      for alias in entry.get("aliases", []):
        index[str(alias).casefold()] = (canonical, entity_type)
    return index

  def _build_alias_pattern(
    self,
    dictionary: dict[str, tuple[str, str]],
  ) -> re.Pattern[str] | None:
    aliases = sorted(dictionary.keys(), key=len, reverse=True)
    if not aliases:
      return None
    body = "|".join(re.escape(alias) for alias in aliases)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)

  def _dedupe(self, tags: list[EntityTag]) -> list[EntityTag]:
    deduped: dict[str, EntityTag] = {}
    for tag in tags:
      canonical, entity_type, confidence, source = tag
      key = canonical.casefold()
      existing = deduped.get(key)
      if existing is None or _tag_rank(tag) > _tag_rank(existing):
        deduped[key] = (canonical, entity_type, confidence, source)
    return list(deduped.values())

  def _load_spacy(self, model: str):
    try:
      import spacy
    except ImportError as exc:
      raise RuntimeError(
        "spaCy is required for NER. Install requirements.txt."
      ) from exc
    try:
      return spacy.load(model)
    except OSError as exc:
      raise RuntimeError(
        f"spaCy model '{model}' is not installed. Run: python -m spacy download {model}"
      ) from exc


def _tag_rank(tag: EntityTag) -> tuple[int, float]:
  _, _, confidence, source = tag
  source_rank = 2 if source == "dictionary" else 1
  return source_rank, confidence


def normalize_ner_canonical(value: str, entity_type: str) -> str:
  normalized = re.sub(r"\s+", " ", value).strip()
  if entity_type in {"person", "place"}:
    return normalized.casefold().title()
  return normalized
