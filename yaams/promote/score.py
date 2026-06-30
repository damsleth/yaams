"""Multi-factor admission scoring for promotion candidates.

See .plans/01-promote-admission-control.md. Pure and dependency-light: scores a
candidate on four independent factors so `promote generate` can rank best-first
and `promote commit --min-score` can gate. Scoring is advisory — it never
auto-rejects; the human review path is unchanged.
"""
from __future__ import annotations

from yaams.trust import PROVENANCE_WEIGHT_FLOOR, PROVENANCE_WEIGHTS, clamp01

# Factor weights, sum 1.0. Overridable via config promote.admission.weights so
# the blend is an A/B surface (report T7).
DEFAULT_WEIGHTS: dict[str, float] = {
  "novelty": 0.35,
  "utility": 0.25,
  "confidence": 0.20,
  "trust": 0.20,
}
# Source-item count at which the corroboration (confidence) factor saturates.
CORROBORATION_TARGET = 6


def _containment(candidate_terms: set[str], reference_terms: set[str]) -> float:
  """Fraction of candidate terms present in the reference vocabulary.

  Containment, not Jaccard: the reference set (all identity + open-loop note
  tokens) is large, so Jaccard would be ~0 regardless of relevance. Containment
  answers the real question — "is this candidate about something already
  central to the user?"
  """
  if not candidate_terms:
    return 0.0
  return len(candidate_terms & reference_terms) / len(candidate_terms)


def admission_score(
  *,
  dedup_similarity: float | None,
  candidate_terms: set[str],
  utility_terms: set[str],
  item_provenances: list[str],
  source_count: int,
  weights: dict[str, float] | None = None,
  corroboration_target: int = CORROBORATION_TARGET,
) -> tuple[float, dict[str, float]]:
  """Return (score in [0,1], per-factor breakdown).

  - novelty:    1 - max embedding similarity to an existing ledger note
                (None similarity -> nothing close -> fully novel).
  - utility:    containment of candidate terms in identity + open-loop notes.
  - confidence: corroboration — source-item count, saturating at the target.
  - trust:      best provenance weight across the source items.
  """
  w = weights or DEFAULT_WEIGHTS
  novelty = 1.0 - clamp01(dedup_similarity or 0.0)
  utility = _containment(candidate_terms, utility_terms)
  confidence = (
    clamp01(source_count / corroboration_target) if corroboration_target > 0 else 0.0
  )
  trust = max(
    (
      PROVENANCE_WEIGHTS.get((p or "").strip().lower(), PROVENANCE_WEIGHT_FLOOR)
      for p in item_provenances
    ),
    default=PROVENANCE_WEIGHT_FLOOR,
  )
  factors = {
    "novelty": novelty,
    "utility": utility,
    "confidence": confidence,
    "trust": trust,
  }
  score = sum(w.get(k, DEFAULT_WEIGHTS[k]) * v for k, v in factors.items())
  return clamp01(score), factors
