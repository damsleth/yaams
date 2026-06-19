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
- untried | blend normalized bm25 magnitude into the item FTS contribution: in _fuse/_stash, for fts entries add a term proportional to (best_bm25 - this_bm25)/range or a softmax over fts_score, weighted small (alpha~0.1-0.2) on top of the rank-RRF | — | — | DIAGNOSIS: fusion throws away bm25 score gaps — a runaway exact lexical match at rank0 is treated like a marginal rank0. `deployment`/`medlemssak`/`sleep...sheep` golds are strong exact term hits that should beat semantically-near cons chatter. Rank-only RRF can't see the lexical confidence. Keep alpha tiny so vec-only golds aren't starved.
- untried | raise tier2_boost for the `factual` shape in route.py (e.g. set cfg.tier2_boost = max(cfg.tier2_boost, 1.6) when parsed.shape=="factual"), mirroring the synthesis/prefer_tier paths | — | — | DIAGNOSIS: the canonical factual answer is repeatedly a tier2_ledger item sitting at rank2-3 under chatty imessage/teams cons: `deployment` (did v0.19 ledger, rank2), `hvem er jeg gift med` (Identitet-Familie, rank2), `sleep...sheep` (rank3). tier2_boost stays 1.2 on bare factual queries; a curated-fact preference for factual intent targets exactly these mrr_partial losses. Watch for regressing factual queries whose gold is NOT tier2.
- untried | sender/participant field RRF credit for person-name factual queries: when parsed query is a person lookup, give items/cons whose sender or participants match the query name tokens a small additive RRF bonus in _hydrate (data already available: row sender/participants, query name tokens) | — | — | DIAGNOSIS: "last messages with gustav meyer-mørch", "when did i last speak with Fredrik Nordmoen" bury the actually-with-that-person doc because the name also appears in content of unrelated docs (gold cons rank6, rrf .055, fts_rank=1 but vec_rank=16). The person identity is a participant signal the current relevance fusion ignores. Scope to person-shaped factual queries to avoid perturbing topic queries.
- untried | sweep OCCURRENCE_RELEVANCE_FLOOR / generalize relevance_floor to factual shape: relevance_floor is only set for first/last_occurrence (0.2); try a small floor (0.05-0.1) on `factual` to prune the weak-tangential tail that pads top-k, OR sweep the occurrence floor itself [0.15,0.25,0.3] | — | — | DIAGNOSIS: factual top-k is padded with rrf≈.032 single-index near-ties (see `medlemssak`, `deployment` lists) that don't displace gold but indicate the score band is flat near the top; a gentle floor could tighten the band so the score-blend/co-coverage ideas above have room to separate gold. Floor never empties a non-empty set, so low recall risk — but verify recall@10 since factual top_k can be small (3-6).

## Tried

- discarded | consolidation_boost_once: apply consolidation_boost to fused cons score once after RRF loop, not per-contribution inside loop | +0.0000 dev (0.5463→0.5463) | — | jun20 round 1. No quality gain; delta=0. Dual-coverage compounding hypothesis did not yield measurable improvement. DO NOT REVISIT.
- discarded | co_coverage_credit: single-index penalty / co-coverage credit in _fuse() — scale rrf_score by f<1 when only one index covers the item (sweep f∈[0.85,0.9,0.95]) | +0.0000 dev (0.5463→0.5463) | — | jun20 round 2. No quality gain; delta=0. RRF already captures the dual-coverage advantage via summed contributions; an explicit scaling factor adds no signal. DO NOT REVISIT.
- kept | per_index_k_resweep: DEFAULT_PER_INDEX_K 80→50 + consolidation_boost 1.1→1.05 | +0.0057 dev (0.5406→0.5463) | — | jun19 round 3. Re-validated on 68-gold set; k=50 outperforms k=80 on the denser set.
- kept | consolidation_boost_resweep: consolidation_boost 1.1→1.05 | +0.0093 dev (0.5313→0.5406) | — | jun19. Temporal regression no longer applies after narrow-date de-boost was gated separately; global knob moved freely and yielded quality gain.
- discarded | RRF_K re-sweep on denser gold set | -0.0093 (3 runs, 1 had a regression) | campaign-jun19 | RRF_K=30 confirmed still optimal on the 68-gold set; every off-30 value lost quality. DO NOT REVISIT.
- parked | event_anchored narrow-window cons de-boost | noisy: -0.0027 then +0.0096 across two runs | campaign-jun19 | within noise on the dev set; +0.0096 doesn't justify the added branch by the simplicity criterion. Revisit only if event-anchored golds grow.

- kept | browse-window fallback for empty time-windowed queries | +0 (fixes zero-result misses, not scored) | b192ca3 | jun19. Fires only on empty results, can't regress.
- kept | entity boost_factor 1.5→3.0 | +0.0047 dev | 74deedd | jun19. Saturates at 3.0 (identical 4/5/8).
- kept | narrow-date (≤3d) consolidation de-boost 0.85 | +0.043 dev (0.4878→0.5313) | 6e3b8a5 | jun19. Window-width gate is essential — flat de-boost regressed the month-range hit.
- discarded | per_field_bm25_sender_weight: sender weight currently 1.0 — lowering it did not reduce false matches on chatty senders | -0.0128 | — | round 2 jun19. FTS_ITEM_WEIGHTS in hybrid.py. DO NOT REVISIT.
- discarded | phrase / NEAR(...,N) clause in _fts_query | +0.000 (neutral) | — | jun19. bm25 over OR-tokens already ranks co-occurrence high; redundant. DO NOT REVISIT.
- parked | subject-token rerank | promising hit_rate but 1 regression | — | revisit only if the regression policy changes to net-neutral.
- parked | large tier2_boost changes | regressed several golds | — | jun8/10.
- discarded | recency decay (10% max penalty) | 4 regressions | — | jun10.
- discarded | vector-k decoupling | failed no-regression floor | — | jun10.
