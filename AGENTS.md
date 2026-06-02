# YAAMS

**Yet Another Agent Memory System.** A high-volume, high-recall memory harness with a `parse → route → retrieve → fuse → answer → log signals` engine. YAAMS is Tier 1 in a two-tier personal memory architecture; the curated Tier 2 lives elsewhere (see below).

This file is the entry point for any agent working in this repo. Read it before touching code.

## What YAAMS is (and isn't)

YAAMS ingests everything: messages, emails, transcripts, documents, browsing context. It normalizes each item to a common schema, embeds, entity-tags, and stores in SQLite. A query interface routes natural-language questions across the store, fuses dense + sparse results, has a local LLM synthesize a grounded answer, and logs structured signals for an offline improvement loop.

- **Append-only.** Raw items are immutable. Edits become new items with `supersedes:` links.
- **Local-only compute.** Embeddings, NER, LLM synthesis run in-environment. No external services.
- **Idempotent ingestion.** Re-running an adapter against the same source produces the same items.
- **Source traceability.** Every answer cites the items it was grounded in.
- **Human review at promotion.** Anything that flows from YAAMS into Tier 2 has been seen and accepted.

YAAMS is not a chat agent, not a continuous screen recorder, not a productivity tool. The output is recall, not action.

## Two-tier memory: where YAAMS sits

Each tier has an **engine** (public code, this repo or a sibling) and a
**store** (private, lives outside the repo, contains your data).

```
Tier 1 (raw, high-volume)          Tier 2 (curated, atomic)
┌──────────────────────────┐       ┌──────────────────────────┐
│ engine: YAAMS            │       │ engine: cognitive-ledger │
│         (this repo)      │       │         (sibling repo)   │
│                          │       │                          │
│ store:  YAAMS SQLite db  │ ────► │ store:  ledger notes     │
│         (private,        │promote│         (private,        │
│          db_path)        │       │          markdown tree)  │
└──────────────────────────┘       └──────────────────────────┘
```

- **YAAMS (engine, public).** This repo. Ingests, normalizes, embeds,
  retrieves, synthesizes. Ships no personal data.
- **YAAMS store (private).** A single SQLite file at `db_path`. Holds the
  ingested items, embeddings, signals. Never committed.
- **cognitive-ledger (engine, public).** Sibling repo. Provides the schema,
  CLIs, and consolidation rules for atomic notes.
- **Ledger store (private).** A markdown tree of curated atomic notes
  produced by cognitive-ledger. **YAAMS reads this as a Tier 2 source via
  a SQLite adapter** (Phase F).

YAAMS is high-volume Tier 1; the ledger is high-precision Tier 2. They
fuse at query time with a small ledger boost.

Do not write into the cognitive-ledger repo or its ledger store from this
repo. Cross-repo writes are out of scope. Reads happen through adapters.

## Engine

```
Query
  │
  ▼
parse ── route ── retrieve ── fuse ── answer ── log signals
                     │           │
            (entity/date filter, hybrid     (RRF, optional
             dense+sparse, cross-tier)       cross-encoder rerank)
```

- **parse**: small LLM call → `ParsedQuery` (shape, entities, date range, topic terms, sort, tier preference).
- **route**: pick retrieval modes based on shape. Five canonical shapes: factual, first_occurrence, temporal_range, synthesis, event_anchored.
- **retrieve**: SQL filter narrows the candidate pool, then hybrid (sqlite-vec + FTS5) over filtered IDs.
- **fuse**: reciprocal rank fusion; merge raw + ledger tiers with a small ledger boost; optional rerank for synthesis queries.
- **answer**: local LLM synthesizes with strict grounding rules. Cites every claim. Surfaces gaps and confidence.
- **log signals**: per-query record + per-feedback signals feed an offline analysis loop (LLM-driven proposals, A/B harness).

Full spec: `.plans/yaams_architecture.md`, `.plans/yaams_phase_a_ingest.md`, `.plans/yaams_phase_b_query_grading.md`.

## Stack

- Python 3.11+
- SQLite (single file) + `sqlite-vec` extension + FTS5
- Embeddings: `bge-m3` via `sentence-transformers` (multilingual, handles Norwegian)
- NER: spaCy `xx_ent_wiki_sm` + dictionary-based entity resolution
- CLI: `click`, config in YAML
- Tests: `pytest`

**LLM backend is pluggable.** The `parse` and `answer` steps go through a thin adapter so any of the following can be the backend: `ollama`, `llama-cpp-python`, `claude`, `codex`, `pi`, `copilot`. Default to whatever is already running locally; don't hard-code a single backend.

## Layout

```
YAAMS/
├── AGENTS.md                  # this file
├── .plans/                    # design docs
├── .tmp/                      # agent scratchpad - gitignored, see Multi-agent section
├── config.yaml
├── yaams/
│   ├── cli.py
│   ├── schema.py              # SQLite schema + migrations
│   ├── db.py
│   ├── ingest/                # one adapter per source (imessage, email, ...)
│   ├── enrich/                # entities, embed
│   ├── retrieve/              # parse, filter, hybrid, fuse, rerank
│   ├── synthesize/            # LLM adapter + synthesis prompts
│   ├── signals/               # query/feedback logging, analysis loop
│   ├── store.py
│   └── watermark.py
├── tests/
└── scripts/                   # init_db, ingest, reset_db, analyze
```

## Phases

- **Phase A** - iMessage + email ingest, end-to-end. Spec: `.plans/yaams_phase_a_ingest.md`.
- **Phase B** - query interface, LLM synthesis, grading loop. Spec: `.plans/yaams_phase_b_query_grading.md`.
- **Phase C** - additional sources (notes, transcripts, browsing, documents).
- **Phase D** - LightMem-style sleep-time consolidation.
- **Phase E** - promotion pipeline into Tier 2 (human-gated).
- **Phase F** - cross-tier query fusion (read the ledger store as a Tier 2 SQLite source).
- **Phase G** - optimization loop (A/B framework, signal-driven config tuning).

Don't skip ahead. Phase A produces the data Phase D needs to consolidate against. Phase B produces the signals Phase G needs to optimize on.

## Multi-agent etiquette

Other agents may be working in this repo at the same time as you - running A/B tests, querying the dataset, ingesting from a new source, drafting plans. Coordination is loose; the rules below keep it from breaking.

- **Diff carefully.** Read `git status` and `git diff` before staging. If you see changes you didn't make, leave them alone. Do not revert work you don't recognize.
- **Never `git add .` or `git add -A`.** Stage by explicit path. Picks up other agents' work-in-progress otherwise.
- **Use `.tmp/` for scratchpad.** Unfinished work, exploratory scripts, intermediate artifacts go here. `.tmp/` is gitignored. Don't put scratchpad files at the repo root.
- **Assume the database is live.** Another agent may be ingesting or running an A/B against the same SQLite file. Use read-only connections (`?mode=ro`) for analysis. For writes, use short transactions; don't hold long-running locks.
- **Long-running runs go in the background.** Ingest passes, eval suites, A/B sweeps - launch them in the background and check back. Don't block the repo on a 40-minute embedding job.
- **Branch for non-trivial changes.** Schema migrations, retrieval-weight changes, prompt rewrites, anything that changes outputs - branch. Trivial fixes on `main` are fine.
- **Don't edit `.plans/` casually.** Those are the design source of truth. Propose changes in a PR or a `.tmp/proposal-*.md` first.
- **Move finished plans to `.plans/done/`.** Once a plan's been implemented (or explicitly abandoned), move the file into `.plans/done/` so the top of `.plans/` only shows what's still live. Keep the filename so cross-references in commits and other plans still resolve.
- **Append to signals, don't rewrite.** The `queries` and `signals` tables are append-only by design. If a record is wrong, write a correcting signal; don't `UPDATE` history.

## Coding conventions

- Python 3.11+. Type hints on public functions. `dataclass` for record types.
- 2-space indentation, no tabs. (Project-wide rule; applies to YAML/JSON/markdown too.)
- No emdash in any text output, code, or commits. Use a regular dash.
- Default to no comments. Only add one when the why is non-obvious.
- Match the language the user initiates in. The dictionary, NER, and embeddings handle both Norwegian and English; assume mixed-language content throughout.
- Research before editing. Never change code you haven't read.

## Quick start for a new agent in this repo

1. Read `.plans/yaams_architecture.md` for the system shape.
2. Read the phase spec for whatever you're working on (`phase_a_ingest.md` or `phase_b_query_grading.md`).
3. `git status` and `git diff` - confirm clean state, or note in-flight work from other agents.
4. Identify which subsystem you're touching (`ingest/`, `retrieve/`, `synthesize/`, `signals/`).
5. Branch if the change is non-trivial. Use `.tmp/` for any scratch files.
6. Stage by explicit path. Never `git add .`.

## Cutting a release

YAAMS ships to PyPI as `yaams` (a sibling of `hugr`, `cognitive-ledger`,
`owa-tools`, and `owa-piggy`). The version lives in two places that must
match: `pyproject.toml` `version` and `yaams/__init__.py` `__version__`.
Pre-1.0, bump the minor for new features and the patch for fixes.

1. Make sure `main` is green: `.venv/bin/pytest -q` and
   `.venv/bin/ruff check yaams tests`.
2. Bump the version in both `pyproject.toml` and `yaams/__init__.py`, and add
   a dated section to `CHANGELOG.md` for the new version (move items out of
   `[Unreleased]`). Keep the existing `[x.y.z]` sections; never rewrite a
   shipped one.
3. Commit as `chore(release): X.Y.Z` (version files + changelog only; land any
   feature/infra commits separately first).
4. Tag it annotated, matching the existing convention (message is the bare
   version): `git tag -a vX.Y.Z -m "X.Y.Z"`.
5. Publish the sdist + wheel to PyPI from the repo `.venv` using `uv`. The
   PyPI API token lives in `UV_PUBLISH_TOKEN` in `./.env` at the repo root (do
   NOT commit it; `.gitignore` already excludes it). `uv publish` reads that
   exact env var name, so sourcing `.env` is enough:
   ```
   rm -rf dist build
   uv build
   set -a && . ./.env && set +a && uv publish dist/*
   ```
   Confirm success at `pypi.org/pypi/yaams/X.Y.Z/json` (200 = live). That JSON
   index lags a few minutes, so a stale 404 right after a successful upload is
   not a failure - do not re-tag or re-build to "fix" it.
6. Push the commit and the tag: `git push origin main && git push origin
   vX.Y.Z`. The tag push triggers `.github/workflows/release.yml`, which
   re-runs the ci.yml gates (lint + tests), rebuilds the wheel + sdist with
   `uv build`, and creates the GitHub Release at the tag with both artifacts
   attached. It does **not** publish to PyPI - the local `uv publish` in
   step 5 is the only thing that uploads there.

CI no longer publishes to PyPI, so the `UV_PUBLISH_TOKEN` repo secret is no
longer used by any workflow. Leave it or remove it with
`gh secret delete UV_PUBLISH_TOKEN --repo damsleth/yaams`; either way, keep the
token in `./.env` for the local `uv publish` in step 5.

If any step fails midway (tag push rejected, PyPI 4xx that is not "File
already exists"), stop and surface the error. Do not force-push a published
tag, and never bump the version a second time to work around an
already-published file.
