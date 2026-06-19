# Autoresearch Idea Ledger

Append-only memory of retrieval experiments across the campaign. A fresh agent
reads this every iteration to (a) avoid re-trying discarded ideas and (b) pick
the next untried lever. `results.tsv` holds the numbers; this holds the *why*.

Format per line: `STATUS | idea | fitness_delta | commit | note`
STATUS ∈ `untried` `kept` `discarded` `crashed` `parked`. New ideas append at the
bottom of **Backlog**; once run, move the line to **Tried** with its verdict.

---

## Backlog (untried — pick the top-value one)

- untried | recall: why do common-term golds ("deployment", "damsleth") land rank=None — is per-index-k too small, or vec/FTS fusion dropping them? | — | — | rank=None means NOT retrieved; reranking can't help. OUT OF SCOPE this campaign (ranking-only) but log findings.
- untried | event_anchored gold-item buried by 1.3 cons boost ("27 april UNE Vibeke") — apply the narrow-window de-boost to event_anchored too? | — | — | risk: other event golds rely on the 1.3 boost; gate on window width like temporal_range.
- untried | per-field bm25: sender weight currently 1.0 — does lowering it reduce false matches on chatty senders? | — | — | FTS_ITEM_WEIGHTS in hybrid.py.
- untried | RRF_K re-sweep on the DENSER gold set (was tuned 60→30 on the sparse set) | — | — | params were "mined out" on the OLD label set; the jun19 densification may have moved the optimum.
- untried | DEFAULT_PER_INDEX_K re-sweep on denser set (was 60→80) | — | — | same rationale — re-validate against 68-gold scenario.
- untried | consolidation_boost default (1.1) re-sweep now that temporal is gated separately | — | — | the global value may now move freely without the temporal regression that blocked it before.

## Tried

- kept | browse-window fallback for empty time-windowed queries | +0 (fixes zero-result misses, not scored) | b192ca3 | jun19. Fires only on empty results, can't regress.
- kept | entity boost_factor 1.5→3.0 | +0.0047 dev | 74deedd | jun19. Saturates at 3.0 (identical 4/5/8).
- kept | narrow-date (≤3d) consolidation de-boost 0.85 | +0.043 dev (0.4878→0.5313) | 6e3b8a5 | jun19. Window-width gate is essential — flat de-boost regressed the month-range hit.
- discarded | phrase / NEAR(...,N) clause in _fts_query | +0.000 (neutral) | — | jun19. bm25 over OR-tokens already ranks co-occurrence high; redundant. DO NOT REVISIT.
- parked | subject-token rerank | promising hit_rate but 1 regression | — | revisit only if the regression policy changes to net-neutral.
- parked | large tier2_boost changes | regressed several golds | — | jun8/10.
- discarded | recency decay (10% max penalty) | 4 regressions | — | jun10.
- discarded | vector-k decoupling | failed no-regression floor | — | jun10.
