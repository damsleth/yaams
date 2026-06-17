"""Unit tests for the trust verdict + provenance core (yaams.trust)."""

from __future__ import annotations

from yaams.trust import (
  PROVENANCE_WEIGHT_FLOOR,
  PROVENANCE_WEIGHTS,
  TrustVerdict,
  derive_provenance,
  effective_confidence,
  trust_verdict,
)

# --- derive_provenance ------------------------------------------------------


def test_derive_provenance_known_sources():
  assert derive_provenance("tier2_ledger") == "curated"
  assert derive_provenance("email") == "authored"
  assert derive_provenance("github") == "structured"
  assert derive_provenance("imessage") == "conversational"
  assert derive_provenance("teams") == "conversational"
  assert derive_provenance("obsidian") == "imported"


def test_derive_provenance_promoted_is_curated():
  assert derive_provenance("imessage", promoted=True) == "curated"


def test_derive_provenance_inferred_timestamp_falls_to_inferred():
  # Unknown source with an inferred timestamp degrades to 'inferred'.
  assert derive_provenance("mystery", timestamp_inferred=True) == "inferred"


def test_derive_provenance_unknown_defaults_conversational():
  assert derive_provenance("mystery") == "conversational"
  assert derive_provenance("") == "conversational"


def test_derive_provenance_case_insensitive():
  assert derive_provenance("EMAIL") == "authored"


# --- effective_confidence ---------------------------------------------------


def test_effective_confidence_applies_weight():
  # imported weight is 0.80; no validations -> 1.0 * 0.80
  assert effective_confidence(1.0, "imported", 0) == 0.80


def test_effective_confidence_validation_boost_capped():
  # 10 validations * 0.03 = 0.30, capped at 0.15
  conf = effective_confidence(0.0, "curated", 10)
  assert conf == 0.15


def test_effective_confidence_unknown_provenance_uses_floor():
  assert effective_confidence(1.0, "???", 0) == PROVENANCE_WEIGHT_FLOOR


def test_effective_confidence_clamped():
  assert effective_confidence(2.0, "curated", 100) == 1.0
  assert effective_confidence(-1.0, "curated", 0) == 0.0


def test_provenance_weights_ordered():
  # curated is the most trusted, inferred the least (above the floor).
  assert PROVENANCE_WEIGHTS["curated"] == 1.0
  assert PROVENANCE_WEIGHTS["inferred"] == PROVENANCE_WEIGHT_FLOOR


# --- trust_verdict ----------------------------------------------------------


def _verdict(**kw) -> TrustVerdict:
  base = dict(
    effective_confidence=0.9,
    validation_count=0,
    contradicted=False,
    superseded=False,
    recency=1.0,
  )
  base.update(kw)
  return trust_verdict(**base)


def test_verdict_superseded_is_low_and_named():
  v = _verdict(superseded=True)
  assert v.level == "low"
  assert "superseded" in v.reason


def test_verdict_contradicted_is_low_and_named():
  v = _verdict(contradicted=True)
  assert v.level == "low"
  assert "contradicted" in v.reason


def test_verdict_supersession_precedes_contradiction():
  v = _verdict(superseded=True, contradicted=True)
  assert "superseded" in v.reason


def test_verdict_high_with_affirmations():
  v = _verdict(effective_confidence=0.9, validation_count=3)
  assert v.level == "high"
  assert "affirmed 3" in v.reason


def test_verdict_high_without_affirmations():
  v = _verdict(effective_confidence=0.9, validation_count=0)
  assert v.level == "high"
  assert v.reason == "high-confidence"


def test_verdict_medium_band():
  v = _verdict(effective_confidence=0.7)
  assert v.level == "medium"


def test_verdict_low_and_stale():
  v = _verdict(effective_confidence=0.3, recency=0.05)
  assert v.level == "low"
  assert "stale" in v.reason


def test_verdict_low_recent():
  v = _verdict(effective_confidence=0.3, recency=0.9)
  assert v.level == "low"
  assert "stale" not in v.reason
