# proposal 0001: jev_a1_junk

- date: 2026-09-25
- verdict: parked
- commit: ef4fb1b
- note: Jev junk labeller: kappa/calibration gates pass (nb rubric 0.674 vs Sonnet self 0.559, ECE 0.127, nb gap 0.113); owner disagreement sheet pending

## diff

```diff
Jev A1 (no code diff to the retrieval path). Scripts: scripts/jev_junk_pass.py, scripts/jev_junk_score.py (commit ef4fb1b).
Question: can Jev noul judge junk on short Norwegian/English chat as well as Sonnet agrees with itself, and is it calibrated?
Result: kappa(Jev nb-rubric @0.5, Sonnet consensus) 0.674 vs Sonnet self-kappa 0.559; like-for-like on all rows Jev 0.515/0.578 vs run1/run2.
Reliability monotonic across all deciles (n >= 641 each), ECE 0.127; Jev over-calls JUNK in the 0.5-0.7 band.
Norwegian penalty (en - nb kappa) 0.113: real but under the 0.15 bar. items.lang tags some Norwegian as en, so the gap is a floor.
Pending: owner verdict on 50 disagreements (~/brain/feed/eval/jev/a1_disagreements.tsv) decides pass/fail.
```
