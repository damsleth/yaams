# Changelog

All notable changes to YAAMS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-`1.0` releases may include breaking changes to the on-disk schema and CLI
surface; pin to a specific version if you need stability.

## [Unreleased]

## [0.1.1] - 2026-05-11

### Added

- `yaams setup` subcommand that installs the spaCy NER models configured
  under `entities.spacy_model` and `entities.spacy_model_nb` into the active
  Python environment. Skips models that are already importable, so reruns
  are cheap. Useful when the Homebrew install-time download is skipped or
  when adding additional language models post-install.

### Changed

- The error raised when a configured spaCy model is missing now points at
  `yaams setup` instead of a raw `python -m spacy download <model>` command.
  The previous suggestion was easy to run against the wrong interpreter
  when YAAMS was installed via Homebrew.
- README leads with `brew install` for the supported install path; the
  source-clone instructions are moved further down.

## [0.1.0] - 2026-05-10

First tagged release. The project has been usable in-tree for several months;
this version cleans up the public-facing surface, adds licensing and security
documentation, and establishes a versioning baseline.

### Added

- **Ingest adapters** for iMessage, Signal Desktop, email (`.emlx` / `.mbox`),
  Obsidian vaults, curated cognitive-ledger notes (Tier 2), GitHub issues
  and PRs, Microsoft Teams (via `owa-piggy`), and Outlook calendar.
- **Storage** in a single SQLite file with FTS5 full-text search, `sqlite-vec`
  dense embeddings, entity tables, and per-source watermarks.
- **Enrichment** via dictionary-based entity tagging plus optional spaCy NER,
  and local embeddings through `sentence-transformers` (`BAAI/bge-m3` by
  default).
- **Query engine** with a `parse -> route -> retrieve -> fuse -> answer ->
  log signals` pipeline, hybrid dense + sparse retrieval, reciprocal rank
  fusion, and a cross-tier boost that fuses Tier 1 raw items with Tier 2
  curated ledger notes.
- **LLM synthesis** with grounded, cited answers and a pluggable backend
  adapter (`claude`, `codex`, `ollama`, `subprocess`, `dummy`).
- **Promotion workflow** that surfaces atomic-note candidates from recent
  items and writes accepted ones into a ledger inbox for human review.
- **Telemetry** in the `ingest_runs` table - one row per (run, source) with
  start time, duration, item counts, status, and error - so nightly runs
  can be diagnosed without parsing log files.
- **CLI**: `init-db`, `ingest`, `stats`, `query`, `feedback`, `signals`,
  `consolidate`, `promote`, `reset-db`, `version`. `yaams --version` works
  globally.
- **Scheduling**: `docs/scheduling.md` describes a nightly `launchd` agent,
  including the macOS Full Disk Access steps required for the `imessage`
  adapter to run under `launchd`.
- **Embed cache UX**: when the Hugging Face cache is missing, YAAMS prompts
  before downloading the embedding model instead of failing with a confusing
  "couldn't connect to huggingface" error.

### Changed

- Config search order is now `$YAAMS_CONFIG`,
  `$XDG_CONFIG_HOME/yaams/config.yaml` (or `~/.config/yaams/config.yaml`),
  then `./config.yaml`. Hardcoded developer-path fallbacks were removed.
- Default `db_path` is `~/yaams/data.db`. Default `promote.inbox_path` is
  `~/yaams/ledger-inbox/`. Both are neutral starting points; configure them
  explicitly in `config.yaml` to match your own layout.

### Security

- Added `SECURITY.md` documenting the threat model, data classification,
  and disclosure flow (GitHub Security Advisories).

[Unreleased]: https://github.com/damsleth/YAAMS/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/damsleth/YAAMS/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/damsleth/YAAMS/releases/tag/v0.1.0
