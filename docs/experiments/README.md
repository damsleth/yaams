# Autoresearch experiment timeline

A self-contained view of every measured retrieval experiment in yaams: what was
tried, what was kept, what was killed, and how the metrics moved over time. The
point is institutional memory — don't re-try a dead idea, and don't mistake
noise for a trend.

> This module is deliberately dependency-free and self-contained so it can be
> lifted into its own repo later (like ux-loop). It assumes nothing about yaams
> except the two upstream TSV ledgers used for the one-time seed.

## Files

| File | Role |
|------|------|
| `experiments.jsonl` | **Source of truth.** One experiment per line, append-only. |
| `index.html` | Self-contained viewer (hand-rolled SVG, zero deps). Opens via `file://`. |
| `build.py` | Inlines `experiments.jsonl` into `index.html`. Run after every append. |
| `seed.py` | One-shot reconstruction of pre-2026-06-30 history from `scripts/autoresearch_*.tsv`. Provenance only. |

## View it

```sh
python docs/experiments/build.py      # refresh the inlined data
open docs/experiments/index.html      # no server needed
```

The line is the **accepted baseline** — it advances only through `keep` wins and
re-measured baselines. `kill` experiments are floating `×` markers off the line.
Shaded bands are gold-set eras; **metrics do not compare across bands** (the gold
set grew 46 → 58 → 78, and the rerank sweep uses full-MRR vs the campaigns'
partial-credit MRR).

## Add an experiment (the rule)

Every run that measures `quality` / `hit_rate` / `mrr` / `recall@10` /
`latency_p95_ms` gets a line — win or lose. Append to `experiments.jsonl`:

```json
{"key":"my_idea","date":"2026-07-01","era":"78 gold (jun21)","disposition":"kill","status":"discard","delta":-0.004,"note":"why it failed","commit":"abc1234","metrics":{"quality":0.62,"hit_rate":0.667,"mrr":0.49,"recall@10":0.92,"latency_p95_ms":171}}
```

Then `python docs/experiments/build.py`.

- `disposition`: `keep` (new accepted baseline) · `kill` (rejected) · `baseline` (re-measure / anchor). Record the **final** disposition — a win later reverted as overfit is a `kill`.
- `era`: bump it whenever the gold set or the metric definition changes.
- Omit / `null` any metric you didn't measure; the viewer skips gaps.
