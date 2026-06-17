# Schema Migration Policy

YAAMS uses a **numbered, journaled migration runtime** (`yaams/migrations/`).
The source of truth for what has been applied is the `schema_migrations` table
(one row per migration name). `SCHEMA_VERSION` in `yaams/schema.py` is now
**informational only** (currently `7`) — it is not the version gate.

Migrations live as individual modules in `yaams/migrations/versions/`, named
`NNNN_<slug>.py`. Each exposes a module-level `name`, an optional
`description`, and an idempotent `apply(conn)` function.

Migration history:

- `0001_baseline` — full schema up to the historical v4 (items, FTS, vector,
  entities, curation layer, consolidations, queries + structured query fields,
  promotion).
- `0002_items_consolidated_into` — `items.consolidated_into` column + index.
- `0003_items_promoted_to` — `items.promoted_to` column.
- `0004_promotion_candidates` — `promotion_candidates` table (+ dedup/conflict
  fields).
- `0005_query_structured_fields` — `parsed_query`, `shape`, `confidence`,
  `confidence_reason`, `gaps`, `parser_fallback`, `provenance` on `queries`.
- `0006_items_provenance` — nullable `items.provenance` (origin-trust class;
  schema v7). See [trust verdicts](#trust-verdicts-and-provenance) below.

## Rules

- Schema changes must be additive when possible.
- Existing raw items are append-only by default. Mutable adapters (Tier 2 ledger,
  Obsidian, GitHub, calendar) refresh canonical fields in place via the upsert
  path in `store._insert_item`; do not rewrite historical rows from a migration.
- New migrations must be deterministic and safe to rerun. Use `IF NOT EXISTS`
  for table/index creation and `PRAGMA table_info` guards for `ALTER TABLE`.
- Migrations must be covered by tests using synthetic data.
- Vector dimension changes require rebuilding `items_vec` and
  `consolidations_vec`.
- Source adapters must continue to produce stable `(source, source_id)` pairs.
- When `SCHEMA_VERSION` is bumped, update this document in the same change.

## Live Tables (schema version 7)

Phase A core:

- `items` (note: the `lang` column has existed since Phase A; it is only
  *populated* for items ingested at ≥ 0.4.0 or via `yaams backfill-lang`. The
  `provenance` column was added at v7; see above.)
- `items_vec`
- `items_fts`
- `entities`
- `item_entities`
- `watermarks`
- `ingest_runs`

Entity curation layer (v5/v6):

- `entity_assoc` — learned co-occurrence (npmi), rebuilt in full by
  `assoc build`
- `entity_relations` — manual assoc links/suppressions
- `entity_tags` — membership tags
- `entity_meta` — key/value attributes, one value per `(entity_id, key)`

Consolidation (Phase D):

- `consolidations`
- `consolidations_vec`
- `consolidations_fts`
- `items.consolidated_into` column

Signals / query logging (Phase B / Phase H):

- `queries` (v4 adds `parsed_query`, `shape`, `confidence`, `confidence_reason`,
  `gaps`, `parser_fallback`)
- `query_results`
- `query_feedback`

Promotion (Phase E):

- `promotion_candidates`
- `items.promoted_to` column

## The migration runtime

`init_schema` (in `yaams/schema.py`) calls `apply_pending(conn)` and then
initializes `items_vec` / `consolidations_vec` (falling back to a plain
BLOB-backed table when `sqlite-vec` is unavailable). `apply_pending`
(`yaams/migrations/__init__.py`):

1. Creates the `schema_migrations(name, applied_at)` journal table if absent.
2. Reads the set of already-applied migration names.
3. **Stamp-on-detect**: if nothing is journaled yet but the database already
   looks like the historical v4 schema (has `items.promoted_to` and
   `promotion_candidates`), it stamps `0001`–`0005` as applied *without*
   re-running them, so existing databases skip straight to the next pending
   migration.
4. `discover()`s all `NNNN_*` modules under `yaams/migrations/versions/`, sorts
   them by name, and runs each pending one inside `BEGIN IMMEDIATE` →
   `apply(conn)` → journal insert → `COMMIT` (rolling back that migration on
   failure).

### Adding a migration

1. Create `yaams/migrations/versions/NNNN_<slug>.py` with `name`,
   `description`, and an idempotent `apply(conn)`. Use `IF NOT EXISTS` for
   table/index creation and `PRAGMA table_info` guards before `ALTER TABLE`
   (see `0006_items_provenance.py` for the column-add pattern).
2. Bump `SCHEMA_VERSION` in `yaams/schema.py` (informational) and add the
   migration to the history list above in the same change.
3. Add a test under `tests/` using synthetic data (a fresh DB reaches the new
   state; a stamped older DB migrates cleanly). See
   `tests/test_migration_provenance.py`.

## Trust verdicts and provenance

`items.provenance` (added in `0006_items_provenance`) records each item's
origin-trust class — derived from its ingest source at write time, e.g. `email`
→ `authored`, `github` → `structured`, `imessage` → `conversational`,
`tier2_ledger` → `curated`. The column is nullable; rows written before the
migration derive their class from `source` at query time. It feeds the
display-only trust verdict attached to query results (see
[user-guide.md](user-guide.md) §5 and `yaams/trust.py`); it never affects
ranking.

## Changing Embedding Models

If the embedding dimension changes, create a migration that:

1. Drops or renames the old vector table(s) (`items_vec`, `consolidations_vec`).
2. Creates new vector tables with the new dimension.
3. Re-embeds existing item and consolidation content in batches.
4. Verifies counts: `items` vs `items_vec`, and `consolidations` vs
   `consolidations_vec` after rebuild.
