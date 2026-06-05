# Schema Migration Policy

YAAMS uses `PRAGMA user_version` as the schema version marker. The current schema version is `6` (`SCHEMA_VERSION` in `yaams/schema.py`).

Version history:

- v4 — structured query fields on `queries` (`parsed_query`, `shape`,
  `confidence`, `confidence_reason`, `gaps`, `parser_fallback`).
- v5 — entity association layer: `entity_assoc`, `entity_relations`.
- v6 — entity metadata: `entity_tags`, `entity_meta`.

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

## Live Tables (schema version 6)

Phase A core:

- `items` (note: the `lang` column has existed since Phase A; it is only
  *populated* for items ingested at ≥ 0.4.0 or via `yaams backfill-lang`)
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

## Additive Migration Guards

`init_schema` (in `yaams/schema.py`) idempotently:

1. Sets `PRAGMA user_version = SCHEMA_VERSION` (currently 6).
2. Creates Phase A tables, the entity curation layer (`entity_assoc`,
   `entity_relations`, `entity_tags`, `entity_meta`), `ingest_runs`,
   and the FTS index with `CREATE TABLE IF NOT EXISTS` /
   `CREATE VIRTUAL TABLE IF NOT EXISTS`.
3. Creates consolidation, queries, query_results, and query_feedback tables.
4. Calls `_migrate_items_consolidated_into` - guards by inspecting
   `PRAGMA table_info(items)`.
5. Calls `_migrate_items_promoted_to` - same guard pattern.
6. Calls `_migrate_promotion_candidates` - `CREATE TABLE IF NOT EXISTS` plus
   indexes.
7. Calls `_migrate_query_structured_fields` - guards `PRAGMA table_info(queries)`
   and adds the Phase H structured columns
   (`parsed_query`, `shape`, `confidence`, `confidence_reason`, `gaps`,
   `parser_fallback`).
8. Initializes `items_vec` and `consolidations_vec`, falling back to a plain
   BLOB-backed table when `sqlite-vec` is unavailable.

Any new migration helper added to `init_schema` must follow the same
guard-and-rerun-safe pattern.

## Changing Embedding Models

If the embedding dimension changes, create a migration that:

1. Drops or renames the old vector table(s) (`items_vec`, `consolidations_vec`).
2. Creates new vector tables with the new dimension.
3. Re-embeds existing item and consolidation content in batches.
4. Verifies counts: `items` vs `items_vec`, and `consolidations` vs
   `consolidations_vec` after rebuild.
