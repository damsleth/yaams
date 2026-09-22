# Retrieval tuning: one honest experiment, end to end

Operator runbook for measuring a change to `yaams/retrieve/*` against the
frozen gold set. It tells you where every file is and which command to run;
it does not restate the code. Read AGENTS.md "Autoresearch experiment log"
first (the rules), then this page (the procedure). The user-facing story
(signals, feedback, review) is in [user-guide.md](user-guide.md#10-the-relevance-loop);
the timeline module and the wiki are in [experiments/README.md](experiments/README.md).

Every step below assumes the **main checkout**, `/Users/damsleth/code/yaams`,
with its venv python (`.venv/bin/python`, Python 3.11; never `uv run`, see the
docstring at `scripts/autoresearch_retrieval.py:27-31`, whose literal
`python3.14` path is stale). Worktrees have no `.venv`, and the harness
resolves its state file and results ledger under its *own* repo root
(`scripts/autoresearch_retrieval.py:68,87-88`), so an anchor established from
a worktree is silently lost.

## 1. Fixture, manifest, eras

| Thing | Where | Set by |
|---|---|---|
| Frozen fixture (SQLite copy of the store, 42 gold) | `~/brain/feed/eval/autoresearch_fixture.db` | `scripts/autoresearch_freeze.py:32` |
| Manifest (fixture path, `source_db`, `gold_hash`, `gold_queries`, `corrections`, `total_queries`) | `scripts/autoresearch_scenario.json` | `scripts/autoresearch_freeze.py:33,81-88` |
| Era label new experiment rows are tagged with | `docs/experiments/CURRENT_ERA` | you, by hand |
| Era 1 fixture backup (79 gold, pre-curation) | `~/brain/feed/eval/autoresearch_fixture_pre-curation-2026-09-17.db` | `.plans/done/data-quality.md` "Promotion 2026-09-17" |

The harness picks its DB in this order: `--db`, `$YAAMS_AUTORESEARCH_DB`, the
fixture above if it exists, `/tmp/yaams_autoresearch.db`, the live config DB
(`scripts/autoresearch_retrieval.py:274-282`). With the fixture in place you
never need `--db` for a code-only variant.

The gold set is the latest `hit`/`correction` feedback row per query that
names a `result_id` (`_load_gold`, `scripts/autoresearch_retrieval.py:132-162`);
`gold_hash` is a SHA1 over those tuples (`scripts/autoresearch_freeze.py:36-51`).
Two freeze paths:

- `scripts/autoresearch_freeze.py` (no flags) snapshots the **live** DB. That
  also pulls in every item ingested since the last freeze, which is a corpus
  change on top of the label change. Use it only when you want both.
- `scripts/autoresearch_freeze.py --promote <db>` promotes an already-prepared
  copy (curated labels, junk annotation, new gold rows) and rewrites the
  manifest. This is how era 2 was made (commit `b6eb4a0`), and how
  `.plans/eval-gold-hygiene.md` task 4 says era 3 will be made.
- `scripts/autoresearch_freeze.py --check` confirms the fixture on disk still
  hashes to the manifest. Run it before an anchor and after any `--db <copy>`
  experiment; a `MISMATCH` means somebody wrote to the fixture.

**Eras are not comparable.** A different gold set (or a different metric
definition) is a different campaign: rank-1 counts, MRR and recall all move
with the label set, not with the code. The viewer bands rows by `era` and
draws no line across a band boundary; `log_experiment.py` reads
`CURRENT_ERA` as the default (`docs/experiments/log_experiment.py:13-15,32-36`).
So **whenever the manifest changes, bump `CURRENT_ERA` in the same commit**,
before logging the new anchor. Precondition for any anchor: the era string
must describe the manifest's `gold_queries` (as of this writing the file still
reads `79 gold (jul01)` against a 42-gold manifest; fix that before logging).
Everything above the `era2-*` rows in `scripts/autoresearch_results.tsv` is
era 1.

The fixture is the promoted *annotated* copy (manifest `source_db` is
`autoresearch_fixture_junk.db`), so `--exclude-junk` runs go straight against
it. The `--exclude-junk` help text at `scripts/autoresearch_retrieval.py:255-257`
("run against a junk-annotated COPY") predates era 2; the copy rule now applies
only to corpus-mutating variants (section 4).

## 2. The anchor and the state file

The regression check compares each gold's rank against the **previous run with
the same key**, read from `scripts/.autoresearch_state.json` (gitignored,
`.gitignore:29`; path at `scripts/autoresearch_retrieval.py:87`). The key is
`"{split}:{mode}"` (`:341`), where mode comes from `_mode_label` (`:230-243`):
`hybrid` or `fts`, plus `+rerank<k>`, `+fb`, `+nojunk` for `--rerank-k`,
`--feedback-boost`, `--exclude-junk`. A recorded run stores per-gold ranks,
`p95_ms`, `recall10` and its tag under that key (`:413-414`).

Why `+nojunk` is its own key: excluding annotated items shrinks the candidate
pool, which shifts every per-index rank. Sharing `dev:hybrid` between
junk-on and junk-off runs cross-compares two corpora and reports phantom
regressions; that is what made the first curated run report 18
(`.plans/done/data-quality.md` "Promotion 2026-09-17"). The same logic is why
`--split test` needs its own anchor: a test-split run has no regression
baseline until one has been recorded under `test:<mode>`.

What the anchor gates, per run (`scripts/autoresearch_retrieval.py:343-366`):

- `regressions`: golds that were rank 1 under the key and no longer are. Any
  regression is `fail:regression`.
- `recall_dropped`: aggregate recall@10 below the anchor's. `fail:recall`.
- p95 latency above 2x the anchor's: `fail:latency`, fitness forced to 0.
  Between 1x and 2x, `fitness = quality - 0.10 * (p95/anchor_p95 - 1)`.

**Delete the state file and re-anchor** when any of these happen:

- the manifest changed (new era);
- the harness changed how it scores (`_EVAL_TOP_K` at `:96`, `recency_now`
  at `:191`, a split or gold-loading change);
- a variant was run **without** `--no-write` and overwrote the anchor with
  its own ranks (`.plans/done/recency-lane.md` "Process note": the lane's
  kill became the anchor until a restore run put it back).

A stale reference produces regressions that are not there (wiki P6). Deleting
the file is safe: keys self-heal on the first recorded run. It drops **every**
key, though, including the plain `dev:hybrid` anchor; re-anchor each mode you
intend to compare under, not just `+nojunk`.

Re-anchor recipe (main checkout, both splits, junk excluded since that is the
live setting):

```sh
cd /Users/damsleth/code/yaams
.venv/bin/python scripts/autoresearch_freeze.py --check
rm -f scripts/.autoresearch_state.json
.venv/bin/python scripts/autoresearch_retrieval.py --split dev  --exclude-junk --tag era2-anchor-nojunk
.venv/bin/python scripts/autoresearch_retrieval.py --split test --exclude-junk --tag era2-anchor-nojunk
```

Recorded runs (no `--no-write`) append to `scripts/autoresearch_results.tsv`,
write the state key, and auto-log to the timeline
(`scripts/autoresearch_retrieval.py:402-423`). **The tag decides the chart
disposition**: a tag containing `anchor` or starting with `baseline` files as
`baseline`, `-keep`/`-win` as `keep`, anything else (including the default
`adhoc`) as `kill` (`docs/experiments/log_experiment.py:49-61`). Name the
anchor accordingly.

## 3. The keep rule

The gate the loop applies (`scripts/autoresearch_loop.workflow.js:198-209`),
which a hand-run experiment applies verbatim:

1. `quality > anchor + MIN_DELTA`, `MIN_DELTA = 0.01`
   (`scripts/autoresearch_loop.workflow.js:26`). Dev jitter is about
   +/-0.006, so anything under 0.01 is noise, not a win.
2. `regressions == 0` (no gold loses rank 1).
3. `recall_dropped == false` (recall@10 floor holds).
4. `p95 <= 2 * anchor p95`.
5. Repeat the dev run once; the number must reproduce.
6. Confirm on `--split test` against the test anchor: same four conditions.
7. **Held-out ablation** for any win within ~2x the noise floor (wiki P4):
   `tier2_boost_fused_order_cap` won dev +0.0106 and lost test -0.058. A win
   later reverted as an overfit is logged as `kill`, not left as `keep`.

`quality = 0.7 * hit_rate + 0.3 * mrr_partial` over the split's gold
(`scripts/autoresearch_retrieval.py:98-101,324-330`); `mrr_partial` counts
correction golds only, so on 13 corrections one flipped correction moves it
noticeably. Read `rank1` alongside `quality`.

## 4. Run recipe

Variant = a change to `yaams/retrieve/*` on a branch (AGENTS.md: retrieval
weight changes branch). Never edit `scripts/autoresearch_retrieval.py`, the
fixture, or the labels as part of a variant.

```sh
cd /Users/damsleth/code/yaams
git switch -c exp/<key>
# edit yaams/retrieve/...
.venv/bin/python scripts/autoresearch_retrieval.py --split dev  --no-write --exclude-junk --json --tag <key>
.venv/bin/python scripts/autoresearch_retrieval.py --split dev  --no-write --exclude-junk --json --tag <key>   # repeat
.venv/bin/python scripts/autoresearch_retrieval.py --split test --no-write --exclude-junk --json --tag <key>
mkdir -p .tmp && git diff main -- yaams/retrieve/ > .tmp/<key>.diff
```

- `--no-write` on **every** variant. It skips the results ledger, the state
  write and the auto-log (`scripts/autoresearch_retrieval.py:402`), so the
  anchor survives and nothing is recorded until you decide the disposition.
  This also means a variant is **never auto-logged**; section 5 is manual.
- `--exclude-junk` because production runs with `retrieve.exclude_junk: true`
  (`.plans/done/data-quality.md` "Promotion 2026-09-17"); compare under the
  setting the user actually queries with. It keys the run to `+nojunk`.
- The harness reads only `retrieve.synonyms` and `retrieve.rerank` from your
  config (`scripts/autoresearch_retrieval.py:270-273,294-300`). Every other
  knob is a CLI flag (`--exclude-junk`, `--feedback-boost`, `--rerank-k`) or
  a code default; a change to your live `config.yaml` does not reach the
  harness (section 6).
- **Corpus-mutating variants** (tokenizer or migration changes, re-annotation,
  new gold rows) run against a copy, never the fixture:
  `cp ~/brain/feed/eval/autoresearch_fixture.db ~/brain/feed/eval/autoresearch_fixture_<key>.db`,
  then `--db ~/brain/feed/eval/autoresearch_fixture_<key>.db`. Run
  `autoresearch_freeze.py --check` afterwards to prove the fixture is intact.
  Their anchor is a fresh recorded baseline on the same copy, since the
  fixture's state keys describe a different corpus.
- Before believing a miss, check the query row's `parsed_query` and
  `parser_fallback` (wiki P6); a degraded parse is a measurement gap, not a
  retrieval gap.

## 5. Recording: win or lose

Two entries per experiment, both from the main checkout. The commands are
documented with their flags in [experiments/README.md](experiments/README.md);
what follows is only what a hand-logged run needs on top.

**Timeline row**, `docs/experiments/log_experiment.py`. Map the harness JSON
the same way the auto-logger does (`docs/experiments/log_experiment.py:87-104`),
or hand rows will not compare with auto rows:

| harness JSON field | `log_experiment.py` flag |
|---|---|
| `fitness` | `--quality` |
| `hit_rate` | `--hit-rate` |
| `mrr_partial` (not `mrr`) | `--mrr` |
| `recall@10` | `--recall10` |
| `retrieval_p95_ms` | `--latency-p95` |
| `status` | `--status` |

```sh
.venv/bin/python docs/experiments/log_experiment.py --key <key> --disposition kill \
  --quality 0.52 --hit-rate 0.5789 --mrr 0.41 --recall10 0.9211 --latency-p95 300 \
  --status ok --delta -0.008 --commit <sha> --note "split=dev regressions=0; why"
```

`--disposition` is the **final** verdict: `keep` moves the accepted-baseline
line, `kill` is a floating point, `baseline` is an anchor. `--era` defaults to
`CURRENT_ERA`; pass it only when back-filling. Rows are append-only: prefer a
correcting row over an edit, and if a hand-edit of `experiments.jsonl` is
unavoidable (a wrong `era` tag), the pre-commit hook rebuilds `index.html`.

**Wiki proposal**, `docs/experiments/wiki.py`, with the diff you captured:

```sh
.venv/bin/python docs/experiments/wiki.py --key <key> --verdict discarded \
  --quality 0.52 --delta -0.008 --commit <sha> --idea "<one line>" \
  --note "why" --diff-file .tmp/<key>.diff
```

Verdicts: `kept`, `discarded`, `crashed`, `apply-failed`, `parked`
(`docs/experiments/wiki.py:29`). Proposal files are immutable; a later revert
is a new entry. If two or more results now point the same way, extend or add a
pattern in `docs/experiments/wiki/patterns.md` (append-mostly, never weaken).

A `keep` additionally: merges the branch, re-runs the recorded anchor under a
`-keep` tag so the state and the baseline line advance, and updates
`CHANGELOG.md` and the config or code comment that documents the knob.

## 6. Live config vs code constants

Two kinds of knob, and they are measured differently.

**Live config** (`retrieve:` block, `config.yaml.example:194-260`). Applied at
query time, by the `yaams query` CLI and the MCP server, never by the harness:
`synonyms` (`yaams/cli/query.py:66-69`), `recency_decay`, `recency_lane`,
`exclude_junk` (helpers at `yaams/cli/query.py:71-105`, called from
`yaams/cli/query.py:436-438` and `yaams/mcp/server.py:128-130`),
`feedback_boost` (`yaams/cli/query.py:434`), `rerank` (`:427-433`). The live config is **one
file**, resolved as `$YAAMS_CONFIG`, then `$XDG_CONFIG_HOME/yaams/config.yaml`
(default `~/.config/yaams/config.yaml`), then `./config.yaml`
(`yaams/config.py:12-34`); there is **no merge** with
`yaams/_default_config.yaml` or `config.yaml.example`, both of which are inert
templates (`.plans/done/retrieval-recall.md` "Runtime activation caveat"). A
synonym group or a default added to the example does nothing for the user
until it is also in their file. On this machine `retrieve.exclude_junk: true`
is live; check the file, not the template, when reasoning about production.

**Code constants** (the surface the loop tunes): `RRF_K`, `DEFAULT_PER_INDEX_K`,
the fetch multipliers and `FTS_ITEM_WEIGHTS` (`yaams/retrieve/hybrid.py:26-51`),
`boost_factor` and the other `HybridQueryConfig` defaults
(`yaams/retrieve/hybrid.py:54-172`), `_THREAD_COHERENCE_OMEGA` and
`_RANK_AGREEMENT_DELTA` (`:783-785`), and the shape routing constants
`SYNTHESIS_TOP_K`, `EVENT_CONS_BOOST`, `TEMPORAL_CONS_BOOST`, `TEMPORAL_NARROW`,
`OCCURRENCE_RELEVANCE_FLOOR` (`yaams/retrieve/route.py:17-36`). These reach
both the CLI and the harness, which is why the harness only needs the flags
listed in section 4.

A config knob (recency lane, recency decay) is measured by changing the
`HybridQueryConfig` default on the branch, since the harness does not apply
the `apply_*_config` helpers. Put it back to default-off before the diff is
recorded unless the verdict is `keep`.

## 7. Known dead ideas

Read `docs/experiments/wiki/patterns.md` before proposing anything; it is the
consolidated list and this page does not duplicate it. In one line each, P1
near-tie ordering swaps, P2 generic coverage credits, P3 global magnitude
tweaks (including recency decay), P4 unablated small dev wins, P5 pool-size
bumps under the current fitness, P6 misses that are really harness or parse
bugs. The per-idea ledger with outcomes is `scripts/autoresearch_ideas.md`;
the raw campaign rows are `scripts/autoresearch_campaign.tsv` and
`scripts/autoresearch_results.tsv`. A kept win in this repo has so far been a
structural signal behind a tight gate (P3), and the binding constraint is
label density, not fusion code (P5).

## Checklist

1. Main checkout, `.venv/bin/python`, `autoresearch_freeze.py --check` is `OK`.
2. `CURRENT_ERA` describes the manifest; bump it first if not.
3. Anchor exists for `dev:hybrid+nojunk` and `test:hybrid+nojunk`, or delete
   the state file and re-anchor with an `anchor` tag.
4. Variant on a branch, `--split dev --no-write --exclude-junk --json`, twice.
5. `--split test --no-write --exclude-junk --json`.
6. Gate: `> anchor + 0.01`, 0 regressions, recall held, p95 within 2x, test
   confirms, held-out ablation if the margin is thin.
7. `log_experiment.py` row with the field mapping, final disposition.
8. `wiki.py` proposal with the diff, win or lose; pattern update if warranted.
