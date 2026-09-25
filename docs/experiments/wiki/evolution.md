# Skill evolution log

Append-only index of every proposal against the skill surface
(`yaams/retrieve/*`), one line per proposal, written by
`docs/experiments/wiki.py`. Each line links the preserved proposal file in
`proposals/`, which carries the full diff and verdict - rejected proposals
included, so later proposals can account for failed attempts.

History predating the wiki (the jun-aug 2026 campaigns) lives in the raw
layer: `scripts/autoresearch_ideas.md` and `scripts/autoresearch_campaign.tsv`.

<!-- entries below are appended by wiki.py; do not hand-edit or reorder -->
- 0001 2026-09-25 [jev_a1_junk](proposals/0001-jev_a1_junk.md) parked - Jev junk labeller: kappa/calibration gates pass (nb rubric 0.674 vs Sonnet self 0.559, ECE 0.127, nb gap 0.113); owner disagreement sheet pending
- 0002 2026-09-25 [jev_a1_junk_final](proposals/0002-jev_a1_junk_final.md) kept - A1 PASS: Jev usable as a junk labeller (kappa 0.674 vs Sonnet self 0.559, calibrated, nb gap 0.113); owner tiebreak 26/50, both judges over-KEEP vs owner; prefer union-of-JUNK
