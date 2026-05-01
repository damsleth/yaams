# YAAMS

Yet Another Agent Memory System. YAAMS is a local-first Tier 1 memory store for high-volume raw personal data. Phase A ingests iMessage and email data, normalizes records into a common SQLite schema, entity-tags them, embeds them, and stores them for later query work.

## Current Status

Phases A, B, C, D, E, and F are implemented:

- iMessage, email, Microsoft Teams, Obsidian vault, Outlook calendar, GitHub, and Tier 2 curated ledger notes ingest
- Idempotent `Item` records with deterministic IDs
- SQLite storage with FTS5, entity tables, watermarks, and sqlite-vec when available
- Dictionary entity tagging plus optional spaCy NER
- Local embeddings through `sentence-transformers`
- Hybrid retrieval (dense + sparse) with reciprocal rank fusion
- Cross-tier query fusion: Tier 2 (curated ledger notes) surface with a configurable boost alongside Tier 1 raw items
- Session consolidation (LightMem-style grouping of conversational items)
- LLM synthesis with grounded, cited answers via a pluggable backend adapter
- Per-query and per-feedback signal logging for an offline improvement loop
- CLI commands for all of the above

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

All commands go through the CLI entry point:

```bash
python -m yaams.cli <command> [options]
```

### Database setup

```bash
python -m yaams.cli init-db
python -m yaams.cli init-db --require-vec   # abort if sqlite-vec is not loaded
```

### Ingest

Dry-run first to verify source access and item counts without writing:

```bash
python -m yaams.cli ingest --dry-run
```

Run ingest (all sources):

```bash
python -m yaams.cli ingest
```

Run a single source:

```bash
python -m yaams.cli ingest --source imessage
python -m yaams.cli ingest --source email
python -m yaams.cli ingest --source notes               # Obsidian vault
python -m yaams.cli ingest --source tier2_ledger        # curated ledger notes (Tier 2)
python -m yaams.cli ingest --source github              # GitHub issues + PRs
python -m yaams.cli ingest --source calendar            # all configured Outlook calendar profiles
python -m yaams.cli ingest --source calendar_swon       # single calendar profile
python -m yaams.cli ingest --source teams_swon
python -m yaams.cli ingest --source teams               # all configured Teams profiles
```

## Ingest sources

| Source | Config key | What it ingests |
|--------|-----------|-----------------|
| `imessage` | `ingest.imessage` | iMessage conversations from local `chat.db` |
| `email` | `ingest.email` | Email from `.emlx` files (Apple Mail) |
| `notes` | `ingest.notes` | Obsidian vault markdown notes |
| `tier2_ledger` | `ingest.tier2_ledger` | Curated atomic notes from cognitive-ledger (Tier 2) |
| `github` | `ingest.github` | GitHub issues and PRs from all your repos (public + private) |
| `calendar` / `calendar_<profile>` | `ingest.calendar` | Outlook calendar events via owa-cal |
| `teams` / `teams_<profile>` | `ingest.teams` | Microsoft Teams messages via Graph API |

### Stats

```bash
python -m yaams.cli stats
```

### Session consolidation

Groups conversational items (iMessage, Teams) into sessions before querying:

```bash
python -m yaams.cli consolidate
python -m yaams.cli consolidate --source imessage
python -m yaams.cli consolidate --dry-run
python -m yaams.cli consolidate --rebuild   # clear and redo all consolidations
```

### Query

Full-text and vector search across all ingested items:

```bash
python -m yaams.cli query "what did Alice say about the project"
python -m yaams.cli query --top-k 20 "budget discussion"
python -m yaams.cli query --no-vector "ATLAS"          # FTS only, no embedder load
python -m yaams.cli query --source imessage "holiday plans"
python -m yaams.cli query --source teams_swon --since 2026-01-01 "onboarding"
python -m yaams.cli query --since 2025-06-01 --until 2025-09-01 "summer"
python -m yaams.cli query --no-consolidations "raw items only"
python -m yaams.cli query --format json "search term"
```

Add `--answer` to synthesize a grounded answer with inline citations using the configured LLM backend:

```bash
python -m yaams.cli query --answer "what are the open items from the ATLAS kickoff"
```

Add `--no-log` to skip writing the query to the signals table.

### Feedback

Log relevance signals against a previous query result (use `query_id` from output):

```bash
python -m yaams.cli feedback <query_id> hit
python -m yaams.cli feedback <query_id> miss -m "expected the June thread"
python -m yaams.cli feedback <query_id> correction --result <item_id> -m "wrong sender"
python -m yaams.cli feedback <query_id> note -m "follow up on this"
```

Feedback kinds: `hit`, `miss`, `correction`, `note`.

### Signals

Inspect recent query history:

```bash
python -m yaams.cli signals
python -m yaams.cli signals --limit 50
```

### Promote

Generate candidate atomic notes from recent items and review them for promotion to the Tier 2 ledger:

```bash
python -m yaams.cli promote generate            # scan last 30 days
python -m yaams.cli promote generate --days 60  # wider window
python -m yaams.cli promote generate --entity "ATLAS"  # single entity
python -m yaams.cli promote list                # show pending candidates
python -m yaams.cli promote list --status accepted
python -m yaams.cli promote review              # interactive review queue
```

In the review loop: `a` accept (writes to ledger inbox), `e` edit in `$EDITOR`, `r` reject, `s` skip, `q` quit.

Accepted candidates are written to `promote.inbox_path` (default `~/yaams/ledger-inbox/00_inbox/`) in the standard ledger note format. Nothing is promoted without your explicit acceptance.

### Reset

```bash
python -m yaams.cli reset-db        # prompts for confirmation
python -m yaams.cli reset-db --yes  # skip prompt
```

## LLM backend

The `--answer` flag in `query` and the future `parse` step use a pluggable LLM adapter configured in `config.yaml`:

```yaml
synth:
  backend: claude          # claude | codex | ollama | subprocess | dummy
  model: claude-sonnet-4-6 # omit to use the CLI or server default
  timeout: 120
```

Available backends:

| backend | how it works | notes |
|---|---|---|
| `claude` | `claude -p --input-format text` via stdin | requires Claude Code CLI |
| `codex` | `codex exec -` via stdin | requires Codex CLI |
| `ollama` | HTTP to `localhost:11434` | set `model` and optionally `host` |
| `subprocess` | pipes to `synth.command` list via stdin | any CLI that reads stdin |
| `dummy` | returns a placeholder, no LLM call | default when `synth:` is absent |

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
