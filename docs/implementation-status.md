# Implementation Status

Date: 2026-05-26
Version: 0.1.10

YAAMS has shipped through its original Phase A (ingest + storage) and Phase B
(query, synthesis, promotion) scope. The full pipeline runs end to end:

```
ingest -> normalize -> embed -> retrieve -> fuse -> answer (cited)
```

## Storage

SQLite schema with the following tables:

- `items`, `items_vec`, `items_fts` - normalized items with vector and
  full-text indexes
- `entities`, `item_entities` - entity dictionary and item links
- `consolidations`, `consolidations_vec` - session-grouped items
- `watermarks`, `ingest_runs` - incremental ingest bookkeeping
- `queries`, `query_results`, `query_feedback` - query history and relevance
  signals
- `promotion_candidates` - Tier 2 promotion queue

`items.provenance` records each item's origin-trust class (derived from its
ingest source) for trust verdicts. Schema changes run through a numbered,
journaled migration runtime (`yaams/migrations/`); see
[schema-migrations.md](schema-migrations.md).

Storage is append-only and idempotent: deterministic item IDs
(`sha256(source:source_id)`) with `INSERT OR IGNORE` on items and replacement
FTS/vector rows. sqlite-vec loads with a plain-table fallback for test and
development environments.

## Ingest

Adapters for:

- `imessage` - local `chat.db` via read-only copy
- `signal` - Signal Desktop (1:1 + groups, attachment metadata)
- `email` - `.emlx` (Apple Mail) and `.mbox`, with attachment extraction and
  quoted-reply / HTML-blockquote trimming
- `notes` - Obsidian vault markdown
- `tier2_ledger` - curated atomic notes from cognitive-ledger
- `chats` - agent chats — Claude Code session summaries written by the
  `SessionEnd` hook (one markdown file per session, with YAML frontmatter)
- `github` - issues and PRs across repos
- `calendar` / `calendar_<profile>` - Outlook via `owa-cal`
- `teams` / `teams_<profile>` - Microsoft Teams via Graph or the `chatsvc`
  engine (for tenants gating Graph behind device-compliance CA policies)
- `m365_mail` - Microsoft 365 mail via `owa-mail`
- `folder` - generic file walker

Entity handling: phrase-aware dictionary matching plus novel-NER support
through `pending_review`.

## Enrich

- Embeddings via `BAAI/bge-m3` (configurable `embed.model`, `embed.device`,
  `embed.models_dir`), offline after first download; fp16 on GPU backends
  (see [embedding-precision.md](embedding-precision.md))
- Entity tagging and retagging (`yaams enrich retag`)

## Retrieve and synthesize

- Hybrid retrieval: FTS5 + vector with fusion (`retrieve/hybrid.py`,
  `retrieve/route.py`)
- Query parsing (`retrieve/parse.py`)
- Cross-tier fusion with a Tier 2 (cognitive-ledger) boost
- LLM synthesis of grounded, cited answers (`synthesize/answer.py`),
  pluggable backend: `claude`, `codex`, `ollama`, `subprocess`, `dummy`
- Display-only trust verdicts on results (`trust.py`, `retrieve/trust.py`):
  high/medium/low derived from provenance, feedback, supersession, and
  recency; never affects ranking

## Consolidation, signals, promotion

- `consolidate` - groups conversational items into sessions
- Signal logging and feedback capture (`feedback`, `signals`)
- `promote` - generate / review / accept atomic-note candidates into the
  Tier 2 ledger inbox; nothing promoted without explicit acceptance

## CLI surface

`init` / `init-db` / `setup` / `reset-db` / `stats` / `version`, plus
`ingest`, `refresh`, `curate`, `query`, `feedback`, `signals`, `consolidate`,
`promote {generate,list,review}`, `entities {list,add,remove,discover,denied,manage}`,
`enrich retag`, `sources` (TUI), `mcp` (MCP server over stdio; see
[mcp-server.md](mcp-server.md)), and `doctor`. Machine-capable action
commands emit byte-identical JSON envelopes under the YAAMS CLI contract;
interactive commands reject `--json` with an actionable error.

## Operations

- `launchd` scheduling for unattended nightly refresh (see
  [scheduling.md](scheduling.md))
- `doctor` for environment and config diagnostics
- Homebrew tap and PyPI/pipx distribution

## Tests

Focused tests across storage, watermarks, entities, all ingest adapters,
parse/route/retrieve, synthesis, consolidation, signals, CLI envelopes, and
config discovery (see `tests/`).
