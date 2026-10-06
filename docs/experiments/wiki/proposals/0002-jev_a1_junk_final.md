# proposal 0002: jev_a1_junk_final

- date: 2026-09-25
- verdict: kept
- commit: ef4fb1b
- note: A1 PASS: Jev usable as a junk labeller (kappa 0.674 vs Sonnet self 0.559, calibrated, nb gap 0.113); owner tiebreak 26/50, both judges over-KEEP vs owner; prefer union-of-JUNK

## diff

```diff
Jev A1 final. Owner tiebreak on 50 blind, shuffled disagreements (nb variant, 13 en + 12 nb per direction):
- owner JUNK on 49/50 (only KEEP: a bare full name)
- Jev JUNK / Sonnet KEEP: owner sides with Jev 25/25
- Jev KEEP / Sonnet JUNK: owner sides with Sonnet 24/25
- total Jev 26/50 = 52% -> pre-registered pass (>= 50%); en 13/26, nb 13/24
Reading: neither judge is better on disagreements; both over-KEEP relative to the owner ("when unsure, KEEP"
in the rubric is looser than the owner's bar). For a junk filter, the union of JUNK verdicts (either judge),
or Jev at tau < 0.5, is closer to owner truth than consensus-of-two.
```
