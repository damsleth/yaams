# proposal 0001: ledger_statement_dedup

- date: 2026-09-25
- verdict: discarded
- quality: 0.5053
- delta: -0.023
- idea: Stop indexing the tier2_ledger statement twice (adapter prepend check never matched real bodies)
- note: paired fixture copies, both re-embedded; dev quality 0.5283->0.5053, rank1 22->21, recall@10 0.921->0.895; test identical (4 golds, 0 tier2). Whole delta is one gold: bare-surname query 'damsleth' -> id__personal_profile rank 1 -> out of top-k (doubled statement was its BM25 TF). tier2_coverage firing 9->8. Low power: 4 tier2 golds in dev. Doubling kept on purpose; revisit at era 3 with >=10 tier2 golds.

## diff

```diff
--- a/yaams/ingest/ledger_notes.py
+++ b/yaams/ingest/ledger_notes.py
-      if statement and not body.lstrip("#\n ").startswith(statement[:40]):
+      if statement and statement[:40] not in body[:400]:
         content = statement + "\n\n" + body
# Measured on paired fixture copies (autoresearch_fixture_junk.db): new arm had the
# leading statement\n\n stripped from 228/230 tier2_ledger items, both arms re-embedded
# through process_batch(reindex=True).
```
