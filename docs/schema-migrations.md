# Schema Migration Policy

Phase A uses `PRAGMA user_version` as the schema version marker. The current schema version is `1`.

## Rules

- Schema changes must be additive when possible.
- Existing raw items are append-only. Do not rewrite historical rows as a migration shortcut.
- New migrations must be deterministic and safe to rerun.
- Migrations must be covered by tests using synthetic data.
- Vector dimension changes require rebuilding `items_vec`.
- Source adapters must continue to produce stable `(source, source_id)` pairs.

## Current Phase A Tables

- `items`
- `items_vec`
- `items_fts`
- `entities`
- `item_entities`
- `watermarks`

Later architecture may add timeline, consolidation, signal, query-cache, and promotion tables. Those belong to later phases unless a phase plan explicitly pulls them forward.

## Changing Embedding Models

If the embedding dimension changes, create a migration that:

1. Drops or renames the old vector table.
2. Creates a vector table with the new dimension.
3. Re-embeds existing item content in batches.
4. Verifies `items` count equals `items_vec` count after rebuild.

