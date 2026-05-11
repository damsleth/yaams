# Privacy And Security

YAAMS stores raw personal communications. Treat the database and any derived artifacts as sensitive.

## Local-Only Defaults

The intended Phase A execution path is local:

- iMessage and email adapters read local files.
- spaCy NER runs in-process.
- `sentence-transformers` embeddings run locally.
- SQLite stores data in `db_path`.

Do not configure cloud embedding, cloud NER, or hosted LLM services for this repo unless a later design explicitly allows it.

## Data Written

Phase A writes:

- normalized items
- message and email text
- sender and recipient identifiers
- subjects and thread hints
- entity links
- embeddings
- watermarks

Phase A does not write to the cognitive-ledger repo or its ledger store.

## Data Not Written

Phase A does not ingest attachments. It records `has_attachments` for iMessage rows when available.

Phase A does not compress, summarize, promote, or consolidate data.

## Operational Rules

- Keep `db_path` outside synced folders unless that is intentional.
- Do not commit SQLite databases, WAL files, Mail exports, or message fixtures containing real personal data.
- Use synthetic fixtures in tests.
- Use `.tmp/` for scratch data.
- Run dry-run before first ingest.
- Use read-only source connections or copied source databases for analysis.

## Deletion And Reset

`python scripts/reset_db.py --yes` deletes the configured YAAMS database only. It does not delete source data.

