# proposal 0003: jev_b2_rerank

- date: 2026-09-25
- verdict: discarded
- quality: 0.5466
- delta: +0.0183
- note: Jev over k=50 pool: blend +0.018 but 2 rank-1 regressions, 0 golds >=5->top3; gate only demotes; replace 0.28; cold p95 1408 ms

## diff

```diff
Jev B2: TypeSafe Jev noul (rel-1) over the hydrated k=50 pool, hook after the rerank block
(yaams/retrieve/hybrid.py `_apply_jev`, spec replace | blend:a | gate:h | filter:tau). Branch jev-query-hook.
Dev, +nojunk, anchor quality 0.5283 (hit_rate 0.5789, mrr_partial 0.4102, recall@10 0.9211).

| spec | quality | hit_rate | mrr_partial | recall@10 | p95 ms | up | down | >=5->top3 | rank1 regr | new rank1 |
|---|---|---|---|---|---|---|---|---|---|---|
| replace (cold) | 0.2808 | 0.2368 | 0.3835 | 0.7632 | 1407.9 | 6 | 24 | 3 | 17 | 4 |
| replace | 0.2508 | 0.2105 | 0.3449 | 0.7895 | 1487.8 | 5 | 25 | 4 | 17 | 3 |
| blend:0.5 | 0.5466 | 0.5789 | 0.4712 | 0.9211 | 743.1 | 7 | 2 | 0 | 2 | 2 |
| blend:1.0 | 0.5423 | 0.5789 | 0.4568 | 0.9211 | 465.6 | 7 | 3 | 0 | 2 | 2 |
| gate:0.8 | 0.4549 | 0.5 | 0.3496 | 0.9211 | 277.4 | 0 | 4 | 0 | 3 | 0 |
| gate:0.9 | 0.4963 | 0.5526 | 0.3647 | 0.9211 | 312.2 | 0 | 1 | 0 | 1 | 0 |

(warm p95s are fusion + cache lookups + any uncached pairs; the cold p95 is the real remote cost)

Verdict: KILL. No variant clears the keep rule (every blend has 2 rank-1 regressions). Blend's +0.014..0.018
is mrr_partial on corrections (7 up, e.g. 'first hear about NOCOS' 26->9 at a=1.0) paid for by swapping two
rank-1 golds down (NOCOS onboarding 1->2, NOCOS standup 1->3/12), with 0 golds from >=5 into the top 3 (P1 shape).
gate never lifted a gold: when rank 1 is right Jev often scores it < 0.5 and a distractor >= h, so it only
demotes. replace confirms the cross-encoder lesson: raw relevance throws away entity/thread/recency signal.
Jev scoring noise: two scorings of the same replace run differ by 0.03 quality.
Cold p95 1408 ms is ~2x the 690 ms gate. By language: nb (6 dev golds) moves only down; too few to call.
Cost: $0.048 for all six runs (cache shared with B4).
```
