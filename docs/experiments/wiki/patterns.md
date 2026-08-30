# Consolidated patterns

Cross-experiment knowledge consolidated from the raw traces
(`scripts/autoresearch_ideas.md`, `scripts/autoresearch_campaign.tsv`,
`docs/experiments/experiments.jsonl`). Read this BEFORE planning any
retrieval experiment; do not propose anything a pattern here marks dead.

Append-mostly: extend a pattern's evidence when a new result confirms it,
append a new pattern when 2+ results point the same way. Never delete or
weaken a pattern because a later proposal failed - failed proposals are what
the patterns are made of. Per-run detail belongs in `proposals/` and the raw
layer, not here.

Seeded 2026-08-30 by consolidating the jun-aug 2026 campaign history.

---

## P1. Ordering-only near-tie adjacent swaps never fire on this gold set

Eight independent variants, all delta=0: the near-tie EPS band (2-10% of
rrf_score) never contains a gold pair, whatever signal picks the swap
direction. The golds that motivate these ideas sit far outside the band
(e.g. `deployment`'s rrf gap is ~80%).

- Evidence: `bm25_margin_tiebreak`, `fts_rank_dominance_tiebreak`,
  `min_rank_near_tie_tiebreak`, `atomic_over_cons_near_tie_tiebreak`,
  `synthesis_cons_near_tie_tiebreak`, `vector_distance_margin_tiebreak`,
  `sole_fts_exact_over_vec_only_tiebreak`, `dual_coverage_disagreement_demote`.
- Implication: stop proposing near-tie swap variants at current label
  density. Revisit the family only if the gold set grows dense enough that
  adjacent near-ties actually contain corrections.

## P2. RRF already prices index coverage; explicit coverage credits add nothing

Crediting or penalizing candidates for how many indexes cover them (or how
well they rank in their sole index) is redundant with RRF's summed
contributions. Generic versions are neutral or regress.

- Evidence: `co_coverage_credit` (0), `consolidation_boost_once` (0),
  `single_index_high_rank_credit` (-0.0054), `fts_only_tier2_recovery` (0),
  `single_modality_depth_credit` (best clean +0.0013, knife-edge and
  non-monotonic, reverted), `graded_rank_agreement` (0).
- The exception that proves the rule: a *targeted* additive recovery with a
  tight blast-radius gate does work - `tier2_factual_coverage_recovery`
  (+0.0487, gated to tier2 AND factual shape AND fts_rank<=5 AND
  vector_rank is None). Narrow gates, not generic coverage signals.

## P3. Wins come from structural signals and tight gates, not global magnitudes

Every kept win either exploits document structure or gates a small adjustment
to a narrow, diagnosable failure mode. Global magnitude tweaks lose or drown
in noise.

- Kept: `narrow_date_cons_deboost` (+0.043, window-width gate essential; the
  flat version regressed), `tier2_factual_coverage_recovery` (+0.0487, see
  P2), `thread_coherence_credit` (+0.0328, structural thread_id link to a
  trusted consolidation), `rank_agreement_multiplier` (+0.0045, mutual
  top-2 agreement), `entity boost_factor 1.5->3.0` (+0.0047, saturates).
- Lost or noise: `bm25_magnitude_blend` (+0.0011 on clean code, discarded by
  the simplicity criterion), `factual_tier2_boost` (-0.0007),
  `lexical_top_hit_step_credit` (-0.0266), `sender_participant_rrf_credit`
  (-0.0043), `recency decay` (4 regressions), synonym-group config additions
  (0).

## P4. Small dev wins overfit; ablate on held-out before trusting anything near the noise floor

Dev-set jitter is ~+/-0.006 and the keep gate's MIN_DELTA=0.01 exists for a
reason. A dev win barely above it can still be a held-out regression.

- Evidence: `tier2_boost_fused_order_cap` won dev +0.0106 and lost test
  -0.058; a post-round ablation localized the all-split regression to this
  single win and it was reverted. `bm25_magnitude_blend`'s original +0.0054
  was a stale-HEAD artifact.
- Implication: any keep within ~2x the noise floor gets a held-out ablation
  before it is trusted, and a reverted overfit is logged as `kill`.

## P5. The binding constraint is the label set, not the fusion code

Most residual misses are not rankable-but-misranked; they are unwinnable or
mislabeled, and tuning fusion for them is wasted rounds.

- Evidence: jul01 manual review of 36 rejudge-eligible misses found 6
  BAD_LABEL (corpus has no answer), 15 MECHANICAL_NOISE (dev/test queries,
  not real info-needs), 6 AMBIGUOUS - only ~9 real retrieval gaps. Buried
  golds like `Nina aksjon korpskveld` contain none of the query terms and are
  absent from vector top-500: bad labels, not fusion failures.
- Bigger candidate pools trade quality for recall: `per_index_k` 50->75/100/200
  raises recall@10 but drops mrr_partial by injecting distractors above
  correction golds; k=50 remains optimal for the quality scalar. Do not bump
  per_index_k unless the fitness is reweighted toward recall.
- Implication: densify and quarantine labels before proposing more fusion
  work; a recall-reweighted fitness is a prerequisite for pool-size ideas.

## P6. Measurement bugs masquerade as retrieval gaps; verify the harness before believing a miss

Several "retrieval failures" were the harness.

- Evidence: replaying at the user's stored top_k truncated golds before rank
  was measured (fixed, _EVAL_TOP_K=50); `--rejudge-misses` replayed the
  original degraded `parsed_query` instead of re-parsing, so recoverable
  misses could never recover (fixed 073c954); a stale regression reference
  in `.autoresearch_state.json` produced phantom regressions that poisoned a
  whole campaign's anchor; a stale-HEAD worktree inflated
  `bm25_magnitude_blend`.
- Implication: before believing a miss, check `parsed_query.entities` /
  `parser_fallback` on the query row. After any harness or gold-set change,
  re-anchor and bump `CURRENT_ERA`. The strict best-of-3 verify gate is doing
  real work (it also rejects BAD_LABEL false positives); do not weaken
  `--votes` to force a flaky gold through.
