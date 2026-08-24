"""Multi-factor admission scoring (.plans/01-promote-admission-control.md)."""
from __future__ import annotations

import json

import pytest

from yaams.db import open_db
from yaams.promote.candidates import (
  PromotionCandidate,
  _is_covered,
  fetch_pending,
  store_candidates,
)
from yaams.promote.score import admission_score
from yaams.schema import init_schema


def _base(**over):
  kw = dict(
    dedup_similarity=0.1,
    candidate_terms={"alpha"},
    utility_terms=set(),
    item_provenances=["authored"],
    source_count=6,
  )
  kw.update(over)
  return kw


def test_near_dup_scores_below_novel():
  dup, _ = admission_score(**_base(dedup_similarity=0.95))
  novel, _ = admission_score(**_base(dedup_similarity=0.10))
  assert dup < novel


def test_utility_overlap_lifts_score():
  hit, f_hit = admission_score(
    **_base(candidate_terms={"nina"}, utility_terms={"nina", "family"})
  )
  miss, f_miss = admission_score(
    **_base(candidate_terms={"zzz"}, utility_terms={"nina", "family"})
  )
  assert hit > miss
  assert f_hit["utility"] == 1.0
  assert f_miss["utility"] == 0.0


def test_empty_utility_terms_is_zero_not_error():
  score, factors = admission_score(**_base(utility_terms=set()))
  assert factors["utility"] == 0.0
  assert 0.0 <= score <= 1.0


def test_curated_provenance_beats_inferred():
  hi, _ = admission_score(**_base(item_provenances=["curated"]))
  lo, _ = admission_score(**_base(item_provenances=["inferred"]))
  assert hi > lo


def test_more_corroboration_raises_confidence():
  _, f_few = admission_score(**_base(source_count=2))
  _, f_many = admission_score(**_base(source_count=6))
  assert f_many["confidence"] > f_few["confidence"]


def _candidate(cid: str, score: float) -> PromotionCandidate:
  return PromotionCandidate(
    id=cid, entity="E", draft_type="fact", draft_title=f"t{cid}",
    draft_statement="s", draft_body="b", draft_tags=["x"],
    source_item_ids=["i1"], admission_score=score,
    admission_factors={"novelty": 0.9, "utility": 0.1, "confidence": 0.5, "trust": 0.9},
  )


def test_score_persists_and_orders_best_first(tmp_path):
  # Round-trip through the DB: also guards the INSERT column count against the
  # 0007 migration, and the best-first ORDER BY in fetch_pending.
  conn = open_db(tmp_path / "y.db")
  init_schema(conn, use_vec=False)
  store_candidates(conn, [_candidate("a", 0.30), _candidate("b", 0.80)])

  rows = fetch_pending(conn, "pending")
  assert [r["id"] for r in rows] == ["b", "a"]
  assert rows[0]["admission_score"] == 0.80
  assert json.loads(rows[0]["admission_factors"])["novelty"] == 0.9


# --- _is_covered word-boundary matching -------------------------------------


@pytest.mark.parametrize(
  "entity,titles,expected",
  [
    # Regression: bare substring containment silently suppressed promotion.
    ("Ada", ["adaptation plan"], False),
    ("AI", ["email strategy"], False),
    ("Ola", ["solar panel notes"], False),
    # Real matches still hit.
    ("Ada", ["meeting with ada today"], True),
    ("AI", ["our ai roadmap"], True),
    ("Crayon", ["crayon group financials"], True),
    # Unicode word chars: Norwegian names must behave.
    ("Håkon", ["lunsj med håkon"], True),
    ("Sørensen", ["sørensen onboarding"], True),
    ("Ås", ["påske plans"], False),
    # Names carrying punctuation are escaped, not treated as regex.
    ("O'Brien", ["call with o'brien"], True),
    ("Marie-Claire", ["marie-claire onboarding"], True),
    ("C++", ["c++ migration"], True),
    # Degenerate input.
    ("", ["anything"], False),
    ("   ", ["anything"], False),
  ],
)
def test_is_covered_matches_on_word_boundaries(entity, titles, expected):
  assert _is_covered(entity, titles) is expected


def test_is_covered_also_checks_the_note_index():
  assert _is_covered("Ada", [], ["ada owns the rollout"]) is True
  assert _is_covered("Ada", [], ["adaptation plan"]) is False
