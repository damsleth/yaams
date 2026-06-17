"""Attach display-only trust verdicts to retrieval results.

Ported from cognitive-ledger's ``attach_trust_verdicts`` (plan 46). Runs
*after* ranking and only annotates results — it never reorders them, so it is
safe to call once the final order is fixed. Each result gets a
``yaams.trust.TrustVerdict`` derived from its provenance class, affirming /
contradicting feedback, supersession (rolled into a consolidation), and
recency. Off when ``trust.show_trust_verdict`` is false; the provenance weight
is applied to the confidence only when ``trust.provenance_weighting_enabled``.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime

from yaams.trust import (
  PROVENANCE_WEIGHTS,
  TrustVerdict,
  clamp01,
  derive_provenance,
  effective_confidence,
  trust_verdict,
)

# Raw Tier-1 items carry no author-asserted confidence: having the item is
# itself high-fidelity evidence of what was said. Provenance weighting (when
# enabled) is what introduces nuance below this ceiling.
RAW_BASE_CONFIDENCE = 1.0
# Recency decay: exp(-age_days / HALF_LIFE). ~2 years old lands under the 0.15
# "stale" threshold that trust_verdict uses for low-confidence items.
RECENCY_HALF_LIFE_DAYS = 365.0


def _recency(timestamp: datetime | None, *, now: datetime) -> float:
  if timestamp is None:
    return 0.0
  ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
  age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
  return clamp01(math.exp(-age_days / RECENCY_HALF_LIFE_DAYS))


def attach_trust_verdicts(
  results: list,
  conn: sqlite3.Connection,
  *,
  show_trust_verdict: bool = True,
  provenance_weighting_enabled: bool = False,
  now: datetime | None = None,
) -> list:
  """Set ``result.trust`` on each result in place and return the same list.

  Never reorders results. No-op (returns unchanged) when *show_trust_verdict*
  is false.
  """
  if not show_trust_verdict or not results:
    return results

  now = now or datetime.now(UTC)

  # Stored provenance + supersession for item results (consolidations have no
  # row in `items`; their provenance is derived from the source below).
  item_ids = [r.id for r in results if getattr(r, "kind", "") == "item"]
  stored: dict[str, tuple[str | None, object]] = {}
  if item_ids:
    placeholders = ",".join("?" for _ in item_ids)
    try:
      rows = conn.execute(
        f"SELECT id, provenance, consolidated_into FROM items WHERE id IN ({placeholders})",
        tuple(item_ids),
      ).fetchall()
      stored = {row[0]: (row[1], row[2]) for row in rows}
    except sqlite3.OperationalError:
      stored = {}  # pre-migration DB without the provenance column

  from yaams.signals.logger import feedback_counts

  counts = feedback_counts(conn, [r.id for r in results])

  for r in results:
    prov_stored, consolidated_into = stored.get(r.id, (None, None))
    provenance = prov_stored or derive_provenance(r.source)
    validations, contradictions = counts.get(r.id, (0, 0))
    superseded = bool(consolidated_into)
    recency = _recency(getattr(r, "timestamp", None), now=now)

    if provenance_weighting_enabled:
      conf = effective_confidence(RAW_BASE_CONFIDENCE, provenance, validations)
    else:
      # No provenance discount: base confidence + validation boost only.
      _ = PROVENANCE_WEIGHTS  # documented dependency; unused when gated off
      conf = clamp01(RAW_BASE_CONFIDENCE + min(0.03 * validations, 0.15))

    verdict = trust_verdict(
      effective_confidence=conf,
      validation_count=validations,
      contradicted=contradictions > 0,
      superseded=superseded,
      recency=recency,
    )
    r.trust = verdict

  return results


def trust_to_dict(trust: object | None) -> dict | None:
  """Serialize a TrustVerdict to a JSON-friendly dict (or None)."""
  if not isinstance(trust, TrustVerdict):
    return None
  return {
    "level": trust.level,
    "reason": trust.reason,
    "score": round(trust.score, 4),
  }
