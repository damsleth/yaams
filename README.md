# YAAMS

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11+-blue)

**Yet Another Agent Memory System.** A local-first, high-recall memory store
for your personal digital exhaust - messages, email, calendar, GitHub
activity, notes - searchable in seconds and synthesizable into grounded,
cited answers by the LLM of your choice.

YAAMS is Tier 1 of a two-tier memory architecture: the firehose. Tier 2 is
the [cognitive-ledger](https://github.com/damsleth/cognitive-ledger): curated
atomic notes you keep forever. YAAMS ingests everything, lets you query
across the lot, and promotes the gems upstream when you're ready.

Each tier is split between a public **engine** and a private **store**:

| Tier | Engine (public) | Store (private, your data) |
| --- | --- | --- |
| Tier 1 | **YAAMS** - this repo | a single SQLite file at `db_path` |
| Tier 2 | **cognitive-ledger** - sibling repo | a markdown tree of curated atomic notes |

Engines ship code, no data. Stores live outside the repo, are gitignored
by default, and never get pushed.

## What

```
                                 query
                                   |
                                   v
   ingest -> normalize -> embed -> retrieve -> fuse -> answer (cited)
      |          |          |                            |
  iMessage    SQLite    BGE-M3                      Tier 2 boost
  Signal     + FTS5    + spaCy                   (cognitive-ledger)
  Email      + vec
  Teams
  Calendar
  GitHub
  Obsidian
```

- **Local-only by default.** Embeddings, NER, and LLM synthesis run on your
  machine. Nothing leaves the host unless you point an adapter at a hosted
  backend.
- **Append-only.** Raw items are immutable. Re-ingest is idempotent.
- **Cited answers.** Every synthesized claim points back to the source items.
- **Pluggable LLM.** `claude`, `codex`, `ollama`, any subprocess, or `dummy`.
- **Single SQLite file.** No daemons, no servers, no cloud bill.

## Why

Language models forget everything between sessions, and the answer is not
"shove more raw chat logs into the context window." The answer is a memory
system you can grep, query, and audit, that lives on your machine and that
you fully control.

YAAMS gives you that for the high-volume side of your life: every iMessage,
every email, every calendar event, every GitHub issue you have touched. It
normalizes them into a common schema, embeds them, and lets you ask
questions like *"what did we decide about the deploy in May?"* and get back
a grounded answer with citations - in seconds, offline.

## Quickstart

Requires Python 3.11+ and macOS (Linux works for everything except the
iMessage adapter).

Homebrew (recommended):

```bash
brew install damsleth/tap/yaams
```

PyPI via pipx:

```bash
pipx install yaams
```

Bleeding-edge from main:

```bash
brew install --HEAD damsleth/tap/yaams
```

Then bootstrap a config and run the dry-run ingest:

```bash
mkdir -p ~/.config/yaams
cp "$(brew --prefix)/share/yaams/config.yaml.example" ~/.config/yaams/config.yaml
$EDITOR ~/.config/yaams/config.yaml
yaams init-db
yaams ingest --dry-run
```

If you cloned the repo instead of installing the package, run the
all-in-one bootstrap script (`scripts/install_phase_a.sh`) - it creates
`.venv`, runs the config wizard, downloads the spaCy NER model, and runs
`init-db` + `ingest --dry-run` in sequence.

Verify the setup:

```bash
yaams --version          # 0.1.1
yaams stats              # zero items - that is expected before first ingest
yaams ingest --dry-run   # see what each adapter would pick up
```

Then do a real refresh:

```bash
yaams refresh
```

The first run downloads the embedding model (`BAAI/bge-m3`, ~2GB). YAAMS
will prompt before downloading; after the cache is populated, subsequent
runs are fully offline. `refresh` runs ingest, then safe entity maintenance
and association rebuilds.

Ask a question:

```bash
yaams query "what did we decide about the deploy in May"
yaams query --answer "open items from the kickoff meeting"
```

## CLI

```
yaams <command> [options]
```

Bare `yaams` lists commands. Global flags: `--version`, `--help`.
Per-command help: `yaams <command> --help`.

For the full feature walkthrough — every command, the entity-curation
workflow, query flags, and best practices — see the
**[User Guide](docs/user-guide.md)**.

| command | what it does |
| --- | --- |
| `init-db` | create the SQLite schema (idempotent) |
| `ingest` | run ingest for all enabled sources (or `--source <name>`) |
| `refresh` | run ingest plus safe entity maintenance and association rebuild |
| `curate` | run the human entity curation workflow |
| `stats` | print item counts per source, plus last ingest run timing |
| `query` | full-text + vector search; `--answer` for synthesized response |
| `feedback` | log relevance signals against a prior query result |
| `signals` | inspect recent query history |
| `consolidate` | group conversational items into sessions |
| `promote` | review and accept atomic-note candidates into the Tier 2 ledger |
| `mcp` | run the MCP server over stdio (exposes query verbs as MCP tools) |
| `reset-db` | drop and recreate the database (asks first) |
| `version` | print version (`--json` for machine-readable) |

### Ingest sources

| source | config key | what it ingests |
| --- | --- | --- |
| `imessage` | `ingest.imessage` | iMessage conversations from local `chat.db` |
| `signal` | `ingest.signal` | Signal Desktop messages (1:1 + groups, attachment metadata) |
| `email` | `ingest.email` | `.emlx` (Apple Mail) or `.mbox` |
| `notes` | `ingest.notes` | Obsidian vault markdown |
| `tier2_ledger` | `ingest.tier2_ledger` | curated atomic notes from cognitive-ledger |
| `chats` | `ingest.chats` | agent chats — Claude Code session summaries (one markdown file per session) |
| `chats_facts` | `ingest.chats_facts` | opt-in tier: atomic facts from chat summaries' `## Insights / Facts` bullets, in isolated indexes (search via `--source chats_facts`) |
| `github` | `ingest.github` | GitHub issues and PRs across your repos |
| `calendar` / `calendar_<profile>` | `ingest.calendar` | Outlook calendar via `owa-cal` |
| `teams` / `teams_<profile>` | `ingest.teams` | Microsoft Teams via Graph API |

Run one source: `yaams ingest --source imessage`.

### Query

```bash
yaams query "budget discussion"
yaams query --top-k 20 "deploy"
yaams query --no-vector "fast FTS-only path"           # skips embedder load
yaams query --source imessage "holiday plans"
yaams query --since 2025-06-01 --until 2025-09-01 "summer"
yaams query --no-consolidations "raw items only"
yaams query --format json "search term"
yaams query --answer "what are the open items from the kickoff"
```

### Promote

Surface candidate atomic notes from recent items, review them interactively,
and write accepted ones into your ledger inbox:

```bash
yaams promote generate            # scan last 30 days (entity-clustered)
yaams promote generate --days 60
yaams promote from-facts          # one candidate per chat-summary fact bullet
yaams promote list                # see the queue
yaams promote review              # interactive: a/e/r/s/q
```

`promote from-facts` reads the `## Insights / Facts` bullets from your chat
summaries directly (no LLM, no entity clustering) and drafts one candidate per
fact; it works whether or not the `chats_facts` retrieval tier is enabled.

Nothing is promoted without your explicit acceptance. Accepted notes land in
`promote.inbox_path` (default `~/yaams/ledger-inbox/`).

## Configure

`config.yaml` lives in the repo root (or `~/.config/yaams/config.yaml`, or
wherever `$YAAMS_CONFIG` points). It is gitignored: it carries your
personal entity dictionary, source paths, and addresses. Edit
`config.yaml.example` instead when contributing structural changes.

Rerun the wizard at any time:

```bash
python scripts/configure_phase_a.py --config config.yaml
```

Key blocks:

```yaml
db_path: ~/yaams/data.db

ingest:
  since: '2025-01-01T00:00:00Z'
  imessage:
    chat_db_path: ~/Library/Messages/chat.db
  email:
    sources:
      - type: emlx
        path: ~/Library/Mail/V10

embed:
  model: BAAI/bge-m3
  device: mps                # or cpu

synth:
  backend: claude            # claude | codex | ollama | subprocess | dummy
  model: claude-sonnet-4-6
```

On Apple Silicon, use the Homebrew arm64 Python explicitly - PyTorch 2.4+
has no x86_64 macOS wheels:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
```

## Scheduling

YAAMS is meant to run unattended. See [docs/scheduling.md](docs/scheduling.md)
for a `launchd` agent that runs a single nightly `yaams ingest` across all
enabled sources, plus the macOS Full Disk Access setup required for the
`imessage` adapter to work under `launchd`.

## Privacy and security

YAAMS reads and stores sensitive personal data. The defaults keep
everything local, but **you are responsible for protecting the database
file at rest** - enable full-disk encryption (FileVault / LUKS / BitLocker)
on the host machine.

See [SECURITY.md](SECURITY.md) for the full threat model, data
classification, and vulnerability disclosure flow. See
[docs/privacy-security.md](docs/privacy-security.md) for the operational
detail on what is written, what is not, and how to scrub.

## MCP server

`yaams mcp` exposes the query verbs as [MCP](https://modelcontextprotocol.io)
tools over stdio, so any MCP client (Claude Desktop, Claude Code, Cursor, …)
can search your Tier-1 store natively:

```bash
pip install 'yaams[mcp]'   # optional extra
yaams mcp                  # read-only: yaams_query, yaams_answer
yaams mcp --allow-write    # also expose the write-gated yaams_feedback tool
```

See [docs/mcp-server.md](docs/mcp-server.md) for tools and client setup.

## Documentation

- **[User Guide](docs/user-guide.md)** - the end-to-end manual: every
  feature, the entity-curation workflow, query power-flags, best practices,
  and troubleshooting.
- [MCP server](docs/mcp-server.md) - run YAAMS as an MCP server; tool reference
  and client configuration.
- [Implementation status](docs/implementation-status.md) - architecture and
  what's shipped.
- [Scheduling](docs/scheduling.md) - unattended nightly ingest via `launchd`.
- [Privacy and security](docs/privacy-security.md) - what's written, what
  isn't, and how to scrub.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the test
suite, schema-migration rules, and commit conventions.

## License

[MIT](LICENSE). Copyright 2026 Carl Joakim Damsleth.
