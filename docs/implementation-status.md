# Implementation Status

Date: 2026-04-29

## Implemented

- Phase A package scaffold under `yaams/`
- SQLite schema for `items`, `items_vec`, `items_fts`, `entities`, `item_entities`, and `watermarks`
- sqlite-vec loading with a plain table fallback for test and development environments
- Deterministic item IDs using `sha256(source:source_id)`
- Idempotent storage with `INSERT OR IGNORE` on items and replacement FTS/vector rows
- iMessage adapter with read-only database copy behavior
- Email adapter for `.emlx` and `.mbox`
- Phrase-aware dictionary entity matching
- Novel NER entity support through `pending_review`
- Watermark read/update helpers
- CLI commands: `init-db`, `ingest`, `stats`, and `reset-db`
- Tests for storage idempotency, watermarks, entity aliases, email parsing, and iMessage parsing

## Deferred

- Query CLI and retrieval API
- LLM parsing and synthesis
- Signal logging and feedback capture
- Attachment extraction
- Quoted email trimming
- Compression and consolidation
- Promotion into Tier 2
- Cross-tier query fusion

## Phase Boundary

Phase A intentionally stops at ingest and storage. Validation is SQL spot checks plus focused tests. Natural-language query behavior belongs to Phase B.

