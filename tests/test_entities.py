from __future__ import annotations

from types import SimpleNamespace

from yaams.enrich.entities import (
  EntityTagger,
  _is_norwegian,
  normalize_ner_canonical,
)


class _FakeEnt:
  def __init__(self, text: str, label: str):
    self.text = text
    self.label_ = label


class _FakeNlp:
  """Stands in for a loaded spaCy pipeline: returns a fixed ent list."""

  def __init__(self, ents: list[tuple[str, str]]):
    self._ents = ents
    self.calls = 0

  def __call__(self, text: str):
    self.calls += 1
    return SimpleNamespace(ents=[_FakeEnt(t, lbl) for t, lbl in self._ents])


def test_dictionary_entities_match_aliases_and_prefer_longest():
  tagger = EntityTagger(
    None,
    [
      {"canonical": "Alice", "type": "person", "aliases": ["Alice Marie"]},
      {"canonical": "Diana", "type": "person", "aliases": ["Em"]},
      {
        "canonical": "Local Aid Society",
        "type": "org",
        "aliases": ["LAS", "Local Aid"],
      },
    ],
  )

  tags = tagger.tag("Alice Marie and Em met Local Aid about the plan.")
  names = {tag[0] for tag in tags}

  assert names == {"Alice", "Diana", "Local Aid Society"}
  assert all(tag[3] == "dictionary" for tag in tags)


def test_ner_person_names_are_case_normalized():
  assert normalize_ner_canonical("ALICE", "person") == "Alice"
  assert normalize_ner_canonical("  alice   marie ", "person") == "Alice Marie"


def test_ner_org_names_preserve_acronyms():
  assert normalize_ner_canonical("NASA", "org") == "NASA"


def test_is_norwegian_detects_nordic_chars():
  assert _is_norwegian("Vi sees på lørdag")


def test_is_norwegian_detects_stopwords_without_nordic_chars():
  # no æ/ø/å, but two distinct Norwegian function words
  assert _is_norwegian("Takk for sist, det var en fin tur")


def test_is_norwegian_rejects_english():
  assert not _is_norwegian("The den was hidden in the forest for years")


def test_norwegian_content_routes_to_nb_model():
  tagger = EntityTagger(None)
  tagger.nlp = _FakeNlp([("London", "GPE")])
  tagger.nlp_nb = _FakeNlp([("Bærum", "LOC")])

  tags = tagger.tag("Vi har øvelse i Bærum på lørdag")

  assert {t[0] for t in tags} == {"Bærum"}
  assert tagger.nlp_nb.calls == 1
  assert tagger.nlp.calls == 0


def test_ner_noise_words_are_dropped_at_tag_time():
  tagger = EntityTagger(None)
  tagger.nlp_nb = _FakeNlp(
    [("IKKE", "ORG"), ("Hei", "LOC"), ("takk!", "ORG"), ("Røde Kors", "ORG")]
  )

  tags = tagger.tag("Hei alle! Glem IKKE øvelsen med Røde Kors. Tusen takk!")

  assert {t[0] for t in tags} == {"Røde Kors"}


def test_markup_is_stripped_before_ner():
  from yaams.enrich.entities import _strip_markup

  s = _strip_markup(
    "Se [referatet](https://example.com/x.pdf) og "
    '<a href="https://redirect.github.com">ny versjon</a> '
    "på https://github.com/user-attachments/abc eller skriv til kim@damsleth.no"
  )
  assert "https://" not in s
  assert "href" not in s
  assert "user-attachments" not in s
  assert "@" not in s
  assert "referatet" in s  # link label survives


def test_ner_artifact_and_fragment_ents_are_dropped():
  tagger = EntityTagger(None)
  tagger.nlp = _FakeNlp(
    [
      ("image](https://github.com", "ORG"),
      ('href="https://redirect.github.com', "ORG"),
      ("Cc", "PERSON"),
      ("r", "ORG"),
      ("EU", "ORG"),  # uppercase acronym survives
      ("NASA", "ORG"),
    ]
  )

  tags = tagger.tag("plain english content")

  assert {t[0] for t in tags} == {"EU", "NASA"}


def test_ner_lowercase_org_canonical_is_capitalized():
  assert normalize_ner_canonical("google", "org") == "Google"
  assert normalize_ner_canonical("stortinget", "org") == "Stortinget"
  # mixed case is intentional, leave it alone
  assert normalize_ner_canonical("iPhone", "org") == "iPhone"


def test_dictionary_hits_survive_noise_filter():
  # a curated dictionary entry wins even if its name is in NOISE_WORDS
  tagger = EntityTagger(None, [{"canonical": "Via", "type": "org"}])
  tagger.nlp_nb = _FakeNlp([("Via", "ORG")])

  tags = tagger.tag("Møtet med Via er på torsdag")

  assert ("Via", "org", 1.0, "dictionary") in tags


def test_exotic_whitespace_and_emoji_are_normalized():
  from yaams.enrich.entities import _strip_markup

  s = _strip_markup("Henrik Slettene møter \U0001f539 Oppmøte kl 10 ✅")
  assert "Henrik Slettene" in s
  assert " " not in s
  assert "\U0001f539" not in s and "✅" not in s
