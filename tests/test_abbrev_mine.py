"""Pattern extraction and shape classification for the abbreviation lane (PR 2).
Pure-function tests on synthetic text — the fixture itself is private."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from abbrev_mine import classify, extract_candidates, initials_of, is_subsequence


def _pairs(text):
  return {(c["short_form"], c["long_form"]) for c in extract_candidates(text)}


def test_long_paren_short():
  got = _pairs("Vi ringte Utlendingsdirektoratet (UDI) i går.")
  assert any(s == "UDI" and l.endswith("Utlendingsdirektoratet") for s, l in got)


def test_short_paren_long():
  got = _pairs("Bruk MCP (Model Context Protocol) for verktøy.")
  assert ("MCP", "Model Context Protocol") in got


def test_equals_pattern():
  assert ("SP", "serviceprovider") in _pairs("husk at SP = serviceprovider i NOCOS")


def test_staar_for():
  assert ("KSOR", "Kvalifisert Søk Og Redning") in _pairs(
    "KSOR står for Kvalifisert Søk Og Redning"
  )


def test_forkortes():
  got = _pairs("Bærum Røde Kors Hjelpekorps forkortes BRKH i referatene")
  assert any(s == "BRKH" for s, _ in got)


def test_paren_noise_rejected_by_subsequence():
  # "(se vedlegg)" is not an expansion of PS; the loose paren patterns
  # must not emit it.
  assert not any(s == "PS" for s, _ in _pairs("PS (se vedlegg) kommer senere"))


def test_subsequence_and_initials_helpers():
  assert is_subsequence("CRAYN", "Crayon")
  assert is_subsequence("AFØR", "Avansert Førstehjelp")
  assert not is_subsequence("XYZ", "Crayon")
  assert initials_of("Bærum Røde Kors") == "brk"


def test_classify_identifier_shapes():
  assert classify("+4794324297", "Nina", "person")[0] == "identifier"
  assert classify("vtv@une.no", "Vibeke Tveit", "person")[0] == "identifier"


def test_classify_initialism_and_contraction():
  rel, _, review = classify("UDI", "Utlendingsdirektoratet", "org")
  assert (rel, review) == ("abbreviation", 1)  # single word -> contraction, not initials
  assert classify("RKH", "Røde Kors Hjelpekorps", "org")[0] == "initialism"
  assert classify("CRAYN", "Crayon", "org")[0] == "abbreviation"


def test_classify_surface_variant_and_nickname():
  assert classify("UDI!", "UDI", "org")[0] == "unknown"  # NER noise, not a relation
  assert classify("jonna", "Jon Andreas", "person")[0] == "nickname"
