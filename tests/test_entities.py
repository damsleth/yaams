from __future__ import annotations

from yaams.enrich.entities import EntityTagger, normalize_ner_canonical


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
