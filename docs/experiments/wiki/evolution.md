# Skill evolution log

Append-only index of every proposal against the skill surface
(`yaams/retrieve/*`), one line per proposal, written by
`docs/experiments/wiki.py`. Each line links the preserved proposal file in
`proposals/`, which carries the full diff and verdict - rejected proposals
included, so later proposals can account for failed attempts.

History predating the wiki (the jun-aug 2026 campaigns) lives in the raw
layer: `scripts/autoresearch_ideas.md` and `scripts/autoresearch_campaign.tsv`.

<!-- entries below are appended by wiki.py; do not hand-edit or reorder -->
- 0001 2026-09-25 [ledger_statement_dedup](proposals/0001-ledger_statement_dedup.md) discarded delta=-0.023 - paired fixture copies, both re-embedded; dev quality 0.5283->0.5053, rank1 22->21, recall@10 0.921->0.895; test identical (4 golds, 0 tier2). Whole delta is one gold: bare-surname query 'damsleth' -> id__personal_profile rank 1 -> out of top-k (doubled statement was its BM25 TF). tier2_coverage firing 9->8. Low power: 4 tier2 golds in dev. Doubling kept on purpose; revisit at era 3 with >=10 tier2 golds.
