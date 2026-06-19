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
- untried | per-field bm25: sender weight currently 1.0 — does lowering it reduce false matches on chatty senders? | — | — | FTS_ITEM_WEIGHTS in hybrid.py.
- untried | DEFAULT_PER_INDEX_K re-sweep on denser set (was 60→80) | — | — | same rationale — re-validate against 68-gold scenario.

## Tried

- kept | consolidation_boost_resweep: consolidation_boost 1.1→1.05 | +0.0093 dev (0.5313→0.5406) | — | jun19. Temporal regression no longer applies after narrow-date de-boost was gated separately; global knob moved freely and yielded quality gain.
- discarded | RRF_K re-sweep on denser gold set | -0.0093 (3 runs, 1 had a regression) | campaign-jun19 | RRF_K=30 confirmed still optimal on the 68-gold set; every off-30 value lost quality. DO NOT REVISIT.
- parked | event_anchored narrow-window cons de-boost | noisy: -0.0027 then +0.0096 across two runs | campaign-jun19 | within noise on the dev set; +0.0096 doesn't justify the added branch by the simplicity criterion. Revisit only if event-anchored golds grow.

- kept | browse-window fallback for empty time-windowed queries | +0 (fixes zero-result misses, not scored) | b192ca3 | jun19. Fires only on empty results, can't regress.
- kept | entity boost_factor 1.5→3.0 | +0.0047 dev | 74deedd | jun19. Saturates at 3.0 (identical 4/5/8).
- kept | narrow-date (≤3d) consolidation de-boost 0.85 | +0.043 dev (0.4878→0.5313) | 6e3b8a5 | jun19. Window-width gate is essential — flat de-boost regressed the month-range hit.
- discarded | phrase / NEAR(...,N) clause in _fts_query | +0.000 (neutral) | — | jun19. bm25 over OR-tokens already ranks co-occurrence high; redundant. DO NOT REVISIT.
- parked | subject-token rerank | promising hit_rate but 1 regression | — | revisit only if the regression policy changes to net-neutral.
- parked | large tier2_boost changes | regressed several golds | — | jun8/10.
- discarded | recency decay (10% max penalty) | 4 regressions | — | jun10.
- discarded | vector-k decoupling | failed no-regression floor | — | jun10.
