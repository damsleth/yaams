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

# nb_core_news_* uses different label names
_NB_LABEL_TYPES = {
  "PER": "person",
  "ORG": "org",
  "LOC": "place",
  "GPE": "place",
}

_NB_CHARS = frozenset("æøåÆØÅ")

# Distinctly Norwegian function words that rarely occur in English prose.
# Backstop for Norwegian text that happens to contain no æ/ø/å; requiring
# two distinct hits keeps English content from misrouting to the nb model.
_NB_STOPWORDS = frozenset({
  "og", "ikke", "jeg", "det", "som", "har", "skal", "kan", "deg", "meg",
  "dere", "hvis", "med", "av", "fra", "til", "takk", "hei", "etter",
  "noen", "alle", "blir", "bare", "denne", "dette", "hva", "hvordan",
})

# Function words, greetings and time terms that NER routinely mis-tags as
# entities. Applied at tag time (NER fallback only — curated dictionary hits
# always win) and shared by the CLI's `discover` / `suggest-prune` junk
# detector.
NOISE_WORDS = {
  # pronouns / function words (NO)
  "var", "hvordan", "ikke", "men", "inn", "deg", "meg", "jeg", "oss",
  "noe", "det", "den", "han", "hun", "her", "der", "fra", "til", "via",
  "ved", "som", "for", "alle", "noen", "hva", "når", "hvor", "også",
  "så", "må", "får", "gjør", "kom", "kommer", "mine", "annen", "ingenting",
  # greetings / interjections (NO + EN)
  "ja", "nei", "ok", "okay", "hei", "hade", "takk", "natta", "sorry",
  "argh", "åja", "yes", "no", "nice", "flink", "hurra", "halla",
  "unnskyld", "supert", "lurt", "viktig", "aldri", "vs", "ah",
  "beskriv", "beskrivelse", "opprette",
  # pronouns / function words (EN)
  "eta", "faks", "unett",
  # email/chat header tokens (xx model tags the CC: field as a person)
  "cc", "bcc", "fwd", "re", "sv", "kopi",
  # temporal terms (NO + EN) - not useful as entities
  "yesterday", "today", "tomorrow", "monday", "tuesday", "wednesday",
  "thursday", "friday", "saturday", "sunday",
  "januar", "februar", "mars", "april", "mai", "juni",
  "juli", "august", "september", "oktober", "november", "desember",
  "january", "february", "march", "june", "july", "october",
  "november", "december",
  "mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag",
  "igår", "idag", "imorgen", "uke", "måned", "år", "week", "month", "year",
  "morning", "evening", "night", "afternoon",
}


def _is_norwegian(text: str) -> bool:
  if _NB_CHARS.intersection(text):
    return True
  words = set(re.findall(r"[a-zæøå]+", text.casefold()))
  return len(_NB_STOPWORDS.intersection(words)) >= 2


def detect_lang(text: str) -> str | None:
  """Return ISO 639-1 language code for text, or None if text is too short."""
  if len(text.strip()) < 10:
    return None
  return "no" if _is_norwegian(text) else "en"


# Characters that never occur inside a legitimate entity name but are common
# in half-stripped markup ("image](https://github.com", 'href="https://...').
_ARTIFACT_CHARS = re.compile(r"[<>\[\](){}|`\"=/\\]|://|@")


def _strip_markup(snippet: str) -> str:
  """Remove URLs and markdown/HTML link syntax before NER. Email and chat
  content arrives with markdown conversions and raw HTML fragments; spaCy
  happily tokenizes those into entities like 'user-attachments' or
  'href="https://redirect.github.com'."""
  # markdown images/links: keep the human-readable label, drop the target
  snippet = re.sub(r"!?\[([^\]]*)\]\(([^)]*)\)", r"\1", snippet)
  # raw URLs and email addresses
  snippet = re.sub(r"(?:https?|ftp)://\S+|www\.\S+", " ", snippet)
  snippet = re.sub(r"\S+@\S+\.\S+", " ", snippet)
  # leftover HTML-ish tags
  snippet = re.sub(r"</?[a-zA-Z][^>\n]*>", " ", snippet)
  # exotic whitespace (nbsp, zero-width, bidi marks) confuses NER span
  # boundaries ('Henrik\\xa0Slettene' tags as two fragments)
  snippet = re.sub(
    "[\u00a0\u200b-\u200f\u2000-\u202f\u205f\u3000\ufeff]", " ", snippet
  )
  # emoji/pictographs get absorbed into entity spans ('\U0001f539 Oppm\u00f8te')
  snippet = re.sub(
    "[\u2190-\u2bff\ufe0e\ufe0f\U0001f000-\U0001faff]", " ", snippet
  )
  return snippet


class EntityTagger:
  def __init__(
    self,
    spacy_model: str | None,
    dictionary: Iterable[dict] | None = None,
    spacy_model_nb: str | None = None,
  ):
    self.dictionary_entries = list(dictionary or [])
    self.dictionary = self._build_alias_index(self.dictionary_entries)
    self.alias_pattern = self._build_alias_pattern(self.dictionary)
    self.nlp = self._load_spacy(spacy_model) if spacy_model else None
    self.nlp_nb = self._load_spacy(spacy_model_nb) if spacy_model_nb else None

  def tag(self, content: str) -> list[EntityTag]:
    results: list[EntityTag] = []
    results.extend(self._tag_dictionary(content))
    if self.nlp is not None or self.nlp_nb is not None:
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
    snippet = content[:5000]
    # strip markdown heading markers so NER doesn't tag them as entities
    snippet = re.sub(r"^#{1,6}\s*", "", snippet, flags=re.MULTILINE)
    snippet = _strip_markup(snippet)
    use_nb = self.nlp_nb is not None and _is_norwegian(snippet)
    nlp = self.nlp_nb if use_nb else self.nlp
    if nlp is None:
      return []
    label_map = _NB_LABEL_TYPES if use_nb else LABEL_TYPES
    doc = nlp(snippet)
    tags: list[EntityTag] = []
    for ent in doc.ents:
      entity_type = label_map.get(ent.label_)
      if entity_type is None:
        continue
      text = ent.text.strip()
      # skip tokens that are purely symbolic (markdown artifacts, punctuation)
      if not re.search(r"\w", text):
        continue
      canonical, resolved_type = self._resolve_dictionary(text)
      if canonical:
        tags.append((canonical, resolved_type, 1.0, "dictionary"))
        continue
      # drop known NER false positives (function words, greetings, time
      # terms) before they enter the entity table; dictionary hits above
      # are curated and always win
      bare = re.sub(r"^\W+|\W+$", "", text, flags=re.UNICODE).casefold()
      if bare in NOISE_WORDS:
        continue
      # markup residue that survived _strip_markup is never a real entity
      if _ARTIFACT_CHARS.search(text):
        continue
      # 1-2 char fragments ('Cc', 'Em', 'r') are junk; keep acronyms (EU, FN)
      if len(bare) <= 2 and not text.isupper():
        continue
      tags.append(
        (normalize_ner_canonical(text, entity_type), entity_type, 0.7, "ner")
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
      # only doc.ents is consumed, so load just tok2vec+ner; skipping the
      # tagger/parser/lemmatizer pipes is ~1.3-1.4x faster with identical
      # entity output (exclude ignores names a model doesn't have)
      return spacy.load(model, exclude=[
        "tagger", "morphologizer", "parser", "lemmatizer",
        "attribute_ruler", "senter",
      ])
    except OSError as exc:
      raise RuntimeError(
        f"spaCy model '{model}' is not installed. Run: yaams setup"
      ) from exc


def _tag_rank(tag: EntityTag) -> tuple[int, float]:
  _, _, confidence, source = tag
  source_rank = 2 if source == "dictionary" else 1
  return source_rank, confidence


def normalize_ner_canonical(value: str, entity_type: str) -> str:
  normalized = re.sub(r"\s+", " ", value).strip()
  # Strip leading/trailing non-word characters so genitive apostrophes,
  # stray backticks, commas and bidi marks don't fork an entity into
  # variants ("Hamas" vs "Hamas'", "`Saksnavn" vs "Saksnavn`"). Internal
  # punctuation (O'Brien, AT&T) is preserved.
  normalized = re.sub(r"^\W+|\W+$", "", normalized, flags=re.UNICODE)
  if entity_type in {"person", "place"}:
    return normalized.casefold().title()
  if normalized.islower():
    # NER picked the surface form from lowercase prose ('google',
    # 'stortinget'); capitalize so the canonical reads as a name and folds
    # into the properly-cased row.
    return normalized[:1].upper() + normalized[1:]
  return normalized
