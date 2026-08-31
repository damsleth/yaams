"""Provenance-weighted confidence and display-only trust verdicts.

Ported from cognitive-ledger's ``ledger/scoring.py`` (plans 42 & 46). The
verdict core (``TrustVerdict`` / ``trust_verdict`` / ``effective_confidence``)
is domain-agnostic and mirrors the ledger verbatim. ``PROVENANCE_WEIGHTS`` and
``derive_provenance`` are adapted to Tier-1 raw exhaust, where an item's
*origin act* is the ingest **source/channel** it arrived through rather than an
author-asserted note field.

Trust verdicts are **display-only**: they collapse several signals into one
human-readable summary per result and never reorder or otherwise feed ranking.
``effective_confidence`` (the provenance-weighted score that drives a verdict)
is gated behind ``trust.provenance_weighting_enabled`` in config — off by
default until A/B validated, matching the ledger's conservative posture.
"""

from __future__ import annotations

from dataclasses import dataclass


def clamp01(value: float) -> float:
  """Clamp *value* to the closed unit interval [0, 1]."""
  if value < 0.0:
    return 0.0
  if value > 1.0:
    return 1.0
  return float(value)


# --- Provenance ------------------------------------------------------------
#
# A Tier-1 item's provenance class is its *origin act*: the kind of channel it
# entered the store through. Weights discount confidence by how trustworthy
# that channel is as a record of fact. Unknown classes fall back to the floor.

PROVENANCE_WEIGHTS: dict[str, float] = {
  "curated": 1.00,  # promoted into the Tier-2 ledger (human-reviewed)
  "authored": 0.92,  # first-party authored records (email / calendar)
  "structured": 0.90,  # structured platform events (github)
  "conversational": 0.82,  # chat (imessage / signal / teams)
  "imported": 0.80,  # bulk imports (obsidian / folder)
  "inferred": 0.70,  # timestamp- or otherwise model-inferred
}
PROVENANCE_WEIGHT_FLOOR: float = 0.70  # fallback for unknown values

# Tier-1 source id -> provenance class. Sources absent here derive a class
# from the source/inferred flags below (see ``derive_provenance``).
_SOURCE_PROVENANCE: dict[str, str] = {
  "tier2_ledger": "curated",
  "email": "authored",
  "calendar": "authored",
  "github": "structured",
  "imessage": "conversational",
  "signal": "conversational",
  "teams": "conversational",
  "obsidian": "imported",
  "folder": "imported",
}


def derive_provenance(
  source: str,
  *,
  timestamp_inferred: bool = False,
  promoted: bool = False,
) -> str:
  """Resolve a Tier-1 item's provenance class from its origin.

  The mapping is keyed on the ingest ``source`` id. A promoted item (one that
  has been accepted into the Tier-2 ledger) is treated as ``curated``. An item
  with no stronger class whose timestamp was inferred degrades to ``inferred``.
  Unknown sources default to ``conversational`` (the common chat case) so they
  still resolve to a sensible weight rather than the unknown floor.
  """
  if promoted:
    return "curated"
  cls = _SOURCE_PROVENANCE.get((source or "").strip().lower())
  if cls is not None:
    return cls
  if timestamp_inferred:
    return "inferred"
  return "conversational"


def effective_confidence(
  base_confidence: float,
  provenance: str,
  validation_count: float,
  *,
  boost_per_signal: float = 0.03,
  boost_cap: float = 0.15,
) -> float:
  """effective = base × provenance_weight + min(boost_per·validations, cap).

  Deterministic and clamped to [0, 1]. ``validation_count`` is the number of
  affirming feedback signals recorded against the item.
  """
  base = clamp01(base_confidence)
  weight = PROVENANCE_WEIGHTS.get((provenance or "").strip().lower(), PROVENANCE_WEIGHT_FLOOR)
  boost = min(max(0.0, boost_per_signal) * max(0.0, validation_count), max(0.0, boost_cap))
  return clamp01(base * weight + boost)


# --- Trust verdict ---------------------------------------------------------


@dataclass(frozen=True)
class TrustVerdict:
  """A human-readable trust assessment for a retrieval result.

  ``level`` is one of ``high`` | ``medium`` | ``low``. ``reason`` is a short
  sentence explaining the level. ``score`` is a continuous [0,1] proxy for
  sorting/debug only — it never feeds ranking.
  """

  level: str  # "high" | "medium" | "low"
  reason: str  # one short human sentence
  score: float = 0.0  # [0,1] continuous, for sorting/debug only


def _affirmations(count: float) -> float | int:
  """Render an affirmation count without a spurious trailing .0."""
  return int(count) if count == int(count) else round(count, 1)


def trust_verdict(
  *,
  effective_confidence: float,
  validation_count: float,
  contradicted: bool,
  superseded: bool,
  recency: float,
  high_confidence: float = 0.85,
  medium_confidence: float = 0.60,
) -> TrustVerdict:
  """Collapse trust signals into a verdict. Pure, deterministic, display-only.

  Precedence:
    1. superseded or contradicted -> ``low`` (names the issue)
    2. high confidence and at least one affirmation -> ``high``
    3. high confidence -> ``high``
    4. moderate confidence -> ``medium``
    5. low confidence and stale -> ``low``
    6. otherwise -> ``low``
  """
  conf = clamp01(effective_confidence)
  if superseded:
    return TrustVerdict("low", "superseded by a newer note", conf * 0.5)
  if contradicted:
    return TrustVerdict("low", "contradicted by another note", conf * 0.5)
  if conf >= high_confidence and validation_count >= 1:
    return TrustVerdict(
      "high", f"high-confidence, affirmed {_affirmations(validation_count)}×", conf
    )
  if conf >= high_confidence:
    return TrustVerdict("high", "high-confidence", conf)
  if conf >= medium_confidence:
    if validation_count >= 1:
      return TrustVerdict(
        "medium", f"moderate confidence, affirmed {_affirmations(validation_count)}×", conf
      )
    return TrustVerdict("medium", "moderate confidence, unaffirmed", conf)
  if recency < 0.15:
    return TrustVerdict("low", "low confidence and stale", conf)
  return TrustVerdict("low", "low confidence", conf)
