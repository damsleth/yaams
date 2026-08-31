"""Drift guard for the memcore fork (cognitive-ledger's shared retrieval package).

`yaams/retrieve/rerank.py`, `yaams/retrieve/trust.py`, and `yaams/trust.py`
are ports of cognitive-ledger code; that code is being extracted into the
installable `memcore` package (ScoredResult, rrf, rerank, trust). Policy
(AGENTS.md, "memcore dependency policy"): never re-fork - import or block.
Until yaams imports memcore directly, this module compares the forked copies
against memcore and FAILS on divergence, so drift is caught instead of
silently accumulating.

Skipped by default: it runs only when `import memcore` succeeds or a sibling
checkout at ../cognitive-ledger/memcore exists. Producing repo:
cognitive-ledger. If a test here fails, the fork has drifted - reconcile
against the ledger first (prefer memcore's behavior when the retrieval
harness says it's neutral or better), then adopt the import.
"""
from __future__ import annotations

import sys
from itertools import product
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SIBLING = _REPO.parent / "cognitive-ledger"


def _load_memcore():
  try:
    import memcore  # type: ignore
    import memcore.rerank  # noqa: F401  (not re-exported by __init__; lazy ST import)
    return memcore
  except ImportError:
    pass
  # A sibling checkout that has the package but isn't installed. Try the
  # layouts a `pip install -e ../cognitive-ledger/memcore` would expose.
  for root in (_SIBLING, _SIBLING / "memcore", _SIBLING / "memcore" / "src"):
    if not (root / "memcore" / "__init__.py").exists():
      continue
    sys.path.insert(0, str(root))
    try:
      import memcore  # type: ignore
      import memcore.rerank  # noqa: F401
      return memcore
    except ImportError:
      sys.path.remove(str(root))
  return None


memcore = _load_memcore()

pytestmark = pytest.mark.skipif(
  memcore is None,
  reason="memcore not available (pip install it or clone cognitive-ledger "
         "as a sibling checkout); drift check skipped",
)


def _sym(name: str):
  """Resolve a memcore symbol from the top level or its trust/rerank modules.

  A missing symbol is a drift failure, not a skip: the seam this repo plans
  to import moved.
  """
  for holder in (memcore, getattr(memcore, "trust", None), getattr(memcore, "rerank", None)):
    if holder is None:
      continue
    # memcore keeps some helpers private (e.g. _clamp01); drift still matters.
    for candidate in (name, f"_{name}"):
      if hasattr(holder, candidate):
        return getattr(holder, candidate)
  pytest.fail(
    f"memcore does not expose {name!r} (looked in memcore, memcore.trust, "
    f"memcore.rerank) - the import seam has moved; update the adoption plan"
  )


def test_clamp01_matches():
  from yaams.trust import clamp01
  theirs = _sym("clamp01")
  for v in (-1.0, -0.001, 0.0, 0.3, 0.9999, 1.0, 1.5, 100.0):
    assert clamp01(v) == theirs(v), f"clamp01({v}) diverged"


# effective_confidence is deliberately NOT in memcore: the extraction (ledger
# 3335193) kept provenance-weighted confidence ledger-side and injects it into
# memcore.trust.attach_trust_verdicts as a `confidence_of` callable. yaams owns
# its own copy in yaams/trust.py; there is no upstream symbol to drift against.


def test_trust_verdict_matches():
  from yaams.trust import trust_verdict
  theirs = _sym("trust_verdict")
  for conf, validations, contradicted, superseded, recency in product(
    (0.0, 0.14, 0.5, 0.6, 0.84, 0.85, 1.0),
    (0, 1, 2.5),
    (False, True),
    (False, True),
    (0.0, 0.14, 0.15, 1.0),
  ):
    kwargs: dict[str, Any] = dict(
      effective_confidence=conf,
      validation_count=validations,
      contradicted=contradicted,
      superseded=superseded,
      recency=recency,
    )
    ours = trust_verdict(**kwargs)
    them = theirs(**kwargs)
    assert (ours.level, ours.reason) == (them.level, them.reason), (
      f"trust_verdict({kwargs}): ours=({ours.level!r}, {ours.reason!r}) "
      f"memcore=({them.level!r}, {them.reason!r})"
    )
    assert ours.score == pytest.approx(them.score), f"trust_verdict({kwargs}) score"


def test_rerank_candidate_text_matches():
  from yaams.retrieve.rerank import candidate_text
  theirs = _sym("candidate_text")
  cases = [
    ("Title", "Body text", 2048),
    ("", "Body only", 2048),
    ("  padded title  ", "  padded body  ", 2048),
    ("Title", "x" * 5000, 2048),
    ("Title", "", 2048),
    ("Tittel med æøå", "brødtekst", 16),
    ("", "", 2048),
  ]
  for title, body, max_chars in cases:
    ours = candidate_text(title, body, max_chars=max_chars)
    them = theirs(title, body, max_chars=max_chars)
    assert ours == them, f"candidate_text({title!r}, len(body)={len(body)}, {max_chars})"


# rerank_pairs needs a real CrossEncoder, so behavioral comparison is out of
# scope here; the model-facing call is exercised at adoption time via the
# retrieval harness (quality must be bit-identical, per the adoption plan).
