from __future__ import annotations

from yaams.enrich.entities import EntityTagger


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

