# Autoresearch wiki - persistent knowledge for skill evolution

The wiki is the persistent knowledge layer of the autoresearch loop, after
WikiSkill ([arXiv 2608.27454](https://arxiv.org/abs/2608.27454)): raw
execution experience, accumulated knowledge, and executable skills are kept
in separate layers, and experience is continuously consolidated into the wiki
so subsequent skill updates build on it. The wiki persists across iterations
even when a skill proposal is rejected - that is the whole point. Rejected
attempts are knowledge, and the audit trail of failed diffs is what stops the
loop from paying for the same dead idea twice.

## The three layers, mapped to yaams

| WikiSkill layer | yaams analog |
|---|---|
| **Raw** (immutable execution traces) | `scripts/autoresearch_results.tsv`, `scripts/autoresearch_campaign.tsv`, `docs/experiments/experiments.jsonl`, the `queries`/`signals` tables |
| **Wiki** (persistent, consolidated knowledge) | this directory: `patterns.md`, `evolution.md`, `proposals/` |
| **Skills** (executable procedural surface) | `yaams/retrieve/*` code and its config defaults |

And the four loop roles:

| WikiSkill role | yaams analog |
|---|---|
| Inference agent | the fan-out experiment agents in `scripts/autoresearch_loop.workflow.js` |
| Wiki maintainer | the per-round maintainer step in the same workflow (consolidates traces into `patterns.md`) |
| Skill proposer | the planner + periodic Opus critic (both read the wiki first) |
| Gating mechanism | the keep/revert gate: quality > anchor + min-delta AND regressions == 0 AND recall floor holds AND p95 within bound |

## Files

| File | Role |
|---|---|
| `patterns.md` | Consolidated cross-experiment knowledge. Append-mostly: patterns gain evidence, they are never deleted or weakened. The proposer reads this first. |
| `evolution.md` | Append-only index of every skill proposal, one line each, written by `../wiki.py`. |
| `proposals/` | One file per proposal: metadata, verdict, and the full diff - rejected proposals included. |

## Rules

- **The wiki survives rejection.** A discarded experiment updates the wiki
  even though the skill surface rolls back. Never delete wiki content because
  the proposal it came from failed.
- **Every proposal is recorded, win or lose.** Use
  `python docs/experiments/wiki.py --key <key> --verdict <verdict> ...
  --diff-file <diff>`. The diff of a rejected proposal is the audit trail
  that lets a later proposal account for the failed attempt.
- **Consolidate, don't transcribe.** `patterns.md` holds cross-experiment
  patterns (2+ results pointing the same way), not per-run logs. Per-run
  detail stays in the raw layer and `proposals/`.
- **Read before proposing.** Any agent planning a retrieval experiment reads
  `patterns.md` before the idea ledger. Do not propose anything a pattern
  marks dead.
- **Cross-tier read.** When the sibling cognitive-ledger repo keeps its own
  wiki patterns file, the autoresearch loop reads it alongside this one under
  the same never-pursue-a-dead-idea rule (candidate paths and the
  `ledgerWiki` override live in `scripts/autoresearch_loop.workflow.js`;
  the cross-read is silently off when no such file exists).
