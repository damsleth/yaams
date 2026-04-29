# YAAMS

Yet Another Agent Memory System. YAAMS is a local-first Tier 1 memory store for high-volume raw personal data. Phase A ingests iMessage and email data, normalizes records into a common SQLite schema, entity-tags them, embeds them, and stores them for later query work.

## Current Status

Phase A is implemented:

- iMessage ingest from a read-only copy of `~/Library/Messages/chat.db`
- Email ingest from `.emlx` trees and `.mbox` files
- Idempotent `Item` records with deterministic IDs
- SQLite storage with FTS5, entity tables, watermarks, and sqlite-vec when available
- Dictionary entity tagging plus optional spaCy NER
- Local embeddings through `sentence-transformers`
- CLI commands for database setup, ingest, stats, and reset

Phase B query, synthesis, feedback, and signal logging are not implemented yet.

## Install

Use Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download xx_ent_wiki_sm
```

On Apple Silicon, `config.yaml` defaults embeddings to `mps`. Change `embed.device` to `cpu` if MPS is not available.

## Configure

Edit [config.yaml](config.yaml) before the first real ingest:

- `db_path`: destination SQLite database
- `ingest.since`: earliest item timestamp to ingest
- `ingest.imessage.chat_db_path`: Messages database path
- `ingest.email.sources`: `.emlx` tree or `.mbox` inputs
- `entities.dictionary`: known people, places, projects, and aliases

Phase A writes only to the YAAMS SQLite database. It does not write to `cognitive-ledger` or `ledger-inbox`.

## Run

Initialize the database:

```bash
python scripts/init_db.py
```

Dry-run ingestion first:

```bash
python scripts/ingest.py --dry-run
```

Run ingest:

```bash
python scripts/ingest.py
```

Check database stats:

```bash
python -m yaams.cli stats
```

## Validate

```bash
pytest -q
```

Spot-check the database with SQLite:

```bash
sqlite3 ~/yaams/data.db "SELECT source, count(*) FROM items GROUP BY source;"
sqlite3 ~/yaams/data.db "SELECT timestamp, sender, substr(content, 1, 80) FROM items ORDER BY timestamp DESC LIMIT 10;"
```

## Privacy

YAAMS is designed for local-only compute. The default stack runs extraction, entity tagging, embeddings, and storage locally. See [docs/privacy-security.md](docs/privacy-security.md) before running against personal data.

