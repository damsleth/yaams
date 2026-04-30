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

To run setup, database initialization, and dry-run ingest in one step:

```bash
scripts/install_phase_a.sh
```

The script creates `.venv`, installs requirements, walks you through Phase A settings with sane defaults, downloads the spaCy model, runs `init_db`, runs `ingest --dry-run`, and prints the remaining real-ingest commands.

Manual setup:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml      # then edit to fit your setup
python scripts/configure_phase_a.py --config config.yaml
python -m spacy download xx_ent_wiki_sm
```

`config.yaml` is gitignored - it carries your personal addresses, paths, and entity dictionary. Track changes by editing `config.yaml.example` instead when contributing structural changes upstream.

On Apple Silicon, use the Homebrew arm64 Python explicitly - PyTorch 2.4+ has no x86_64 macOS wheels, so a Rosetta Python will be stuck on torch 2.2 and fail at embedding time:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
```

On Apple Silicon, `config.yaml` defaults embeddings to `mps`. Change `embed.device` to `cpu` if MPS is not available.

iMessage rows with plain `text` ingest normally. Rows that only contain Apple's binary `attributedBody` are decoded through PyObjC/Foundation on macOS; dry-run stats report how many binary bodies were decoded or skipped.

## Configure

The installer runs an onboarding wizard for [config.yaml](config.yaml). To rerun it:

```bash
python scripts/configure_phase_a.py --config config.yaml
```

Press Enter through the prompts to accept the batteries-included defaults:

- `db_path`: destination SQLite database
- `ingest.since`: earliest item timestamp to ingest
- `ingest.imessage.chat_db_path`: Messages database path
- `ingest.email.sources`: `.emlx` tree or `.mbox` inputs
- `entities.dictionary`: known people, places, projects, and aliases
- `embed.device`: `mps` on Apple Silicon, otherwise `cpu`

### Email Sources

The default email source is Apple Mail's local store. The wizard uses the newest `~/Library/Mail/V*` directory it finds:

```yaml
ingest:
  email:
    sources:
      - type: emlx
        path: ~/Library/Mail/V10
```

This is the native macOS Mail.app location. Outlook for Mac uses a separate profile/cache location under `~/Library/Group Containers/UBF8T346G9.Office/...`; YAAMS does not read that directly in Phase A.

If you want Outlook mail included now, sync the same accounts into Mail.app and let YAAMS read the Apple Mail `.emlx` store, or export mail to `.mbox` and point `ingest.email.sources` at that file.

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

Run ingest (all sources):

```bash
python scripts/ingest.py
```

Run a single source:

```bash
python scripts/ingest.py --source imessage
python scripts/ingest.py --source email
```

`--source` accepts `all` (default), `imessage`, or `email`. Add `--require-vec` to abort if sqlite-vec is not loaded.

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
