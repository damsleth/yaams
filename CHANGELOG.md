# Changelog

All notable changes to YAAMS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-`1.0` releases may include breaking changes to the on-disk schema and CLI
surface; pin to a specific version if you need stability.

## [Unreleased]

## [0.3.2] - 2026-06-02

### Changed

- Teams channel ingestion now retries `owa-teams` on a 429 rate limit with
  exponential backoff (1, 2, 4, 8, 16s, capped 30s) instead of silently
  dropping a team's channels. The fan-out bursts enough calls to trip chatsvc's
  limiter, and `owa-teams` exits non-zero rather than waiting. Tune with
  `ingest.teams_channels.max_retries` (default 5; `0` disables). A tighter
  `teams` allowlist remains the best way to avoid 429s in the first place.

## [0.3.1] - 2026-06-02

### Added

- Teams **channel** ingestion: a new `teams_channels_<profile>` source ingests
  channel posts and threaded replies by shelling out to the `owa-teams` CLI —
  the same thin-adapter pattern as `calendar` (owa-cal) and `mail` (owa-mail),
  and distinct from the existing `teams_<profile>` chat source so routing and
  watermarks separate channels from chats. Threads cluster a root with its
  replies via owa-teams' `rootMessageId`. Off by default; enable per profile
  under `ingest.teams_channels` (or the `yaams sources` TUI) and set a `teams`
  allowlist to bound the per-team subprocess fan-out.

## [0.3.0] - 2026-06-02

### Added

- M365 people import: `yaams entities import-people` pulls the authenticated
  user, personal contacts, and directory/relevance search results (via
  owa-people) into the entity dictionary, mapping each person to a canonical
  name plus email aliases so NER resolves colleagues across every source.
  Existing entries gain new aliases; nothing is removed. A denied owa-people
  scope degrades to a warning as long as another surface returns people.
- End-user guide (`docs/user-guide.md`), linked from the README.

### Changed

- YAAMS now publishes to PyPI: `pipx install yaams` (or `uv pip install
  yaams`). Releases publish automatically on a `v*` tag push via the `publish`
  GitHub Actions workflow; the local release runbook lives in `AGENTS.md`.

## [0.2.0] - 2026-06-01

### Added

- Entity junk detector: `yaams entities suggest-prune` flags NER false
  positives (stopwords, all-lowercase fragments, very-short non-acronyms,
  numerics, symbol-heavy strings) with reasons and item counts for review.
  Curated and denied entities are excluded; nothing is auto-pruned.
- Entity cleanup pass: `entities merge`, `entities prune`, and merge
  suggestions, plus an interactive dedupe TUI for reviewing merge candidates.
- Auto-normalization of punctuation-only entity variants.
- Custom entity metadata: free-form tags and key/value attributes.
- Retrieval entity association layer: learned co-occurrence plus manual links.
- Synonym expansion of FTS queries from entity aliases.
- First-class date sorting for temporal-locator queries.

## [0.1.13] - 2026-06-01

### Changed

- Ingest now fetches all sources concurrently (network-bound owa-* and Graph
  calls) and then embeds/stores serially, collapsing total wall-clock from the
  sum of per-source latency toward the slowest single source.
- M365 mail ingest uses `owa-mail --all` to follow Graph pagination instead of
  walking the date range in fixed chunks — one subprocess per folder regardless
  of message count.
- Ingest skips already-stored items before embedding and entity-tagging, so a
  run that re-sees only known items does no embedding work and never loads the
  embedding model. A no-op full ingest drops from ~10s to ~3s.
- Teams ingest lists chats newest-message-first (`$orderby` on
  `lastMessagePreview/createdDateTime`) and stops paging at the first chat
  older than the cutoff, instead of enumerating every chat each run. Falls
  back to the unordered listing if a tenant rejects the ordering.

### Fixed

- Mail watermarks now advance past messages that were scanned but skipped (e.g.
  newsletters), so all-skip profiles no longer re-walk their entire date window
  on every run.
- `owa-mail --all` removes a silent truncation: the previous chunked path
  capped each window at 200 results with no pagination, dropping anything past
  the cap.

## [0.1.12] - 2026-05-31

### Added

- Signal review now runs a scan-and-judge loop with a noise cascade and
  provenance tracking, so low-signal items are filtered before judging.
- Display-time formatting helpers for retrieval results, with improved
  rendering of consolidation results and participant formatting.

### Changed

- Mail ingest uses `owa-mail --with-body` and now surfaces silently dropped
  messages in the run stats.
- `embed` supports a quiet mode that suppresses progress bars.
- The `hugr-conventions` dependency was dropped in favour of an inline
  vendored contract.

### Fixed

- Review TUI inherits the terminal foreground/background colours instead of
  forcing white-on-black.
- Review result snippets use a longer length for better context.

## [0.1.11] - 2026-05-26

### Security

- Signal ingest now validates that the SQLCipher key is pure hex before
  interpolating it into the `PRAGMA key` script fed to the `sqlcipher`
  CLI, refusing keys that could break out of the quoting.

### Changed

- Config is validated at load time: top-level `ingest`/`embed`/`synth`/
  `entities` sections must be mappings, and numeric knobs (`embed.batch_size`/
  `dimension`, `synth.timeout`, `mail.chunk_days`/`page_size`,
  `teams.page_size`, `calendar.chunk_days`) must be positive numbers. A
  mistyped value now fails with a message naming the key instead of a
  cryptic error mid-run.
- `owa-mail` subprocess failures and non-JSON output during M365 mail
  ingest are now logged instead of silently returning empty.
- Folder ingest logs PDF/DOCX read failures (and EXIF/frontmatter parse
  failures at debug) instead of swallowing them; missing optional
  dependencies still skip silently.

## [0.1.10] - 2026-05-26

### Fixed

- Teams chatsvc messages now populate `recipients` (with a subject
  fallback) and include the local user in recipients when another
  participant is the sender.

## [0.1.9] - 2026-05-26

### Added

- Obsidian vault is now a first-class ingest source. `ingest.notes`
  ships in the default config and `config.yaml.example`, and the
  `yaams sources` TUI synthesizes a `notes` row when missing — press
  `a` to set `vault_path` (lazy-creates the block) and toggle from
  there.
- Teams chatsvc engine. `ingest.teams.engine_overrides` lets a profile
  use the `teams.microsoft.com/api/chatsvc` API instead of Graph
  `/me/chats` for tenants that gate Graph behind device-compliance CA
  policies. Optional `chatsvc_region` (default `emea`).

### Changed

- `yaams ingest --source notes` now raises a clear error when
  `vault_path` is unset instead of a bare `KeyError`.

## [0.1.8] - 2026-05-25

### Added

- Microsoft 365 mail ingester. `mail_<profile>` source shells out to
  `owa-mail` to pull Inbox + SentItems per owa-piggy profile, fetches
  full bodies via `owa-mail show`, and reuses the mbox body cleaner so
  output matches the local emlx ingester. Configure under
  `ingest.mail.profiles` in `config.yaml`.
- `yaams sources` synthesizes `mail`, `calendar`, and `teams` rows
  whenever owa-piggy reports any enabled profile, even when the YAML
  doesn't yet have the source block. First toggle (parent or profile
  child) writes a default block, so users no longer have to hand-edit
  YAML before the TUI can manage M365 sources.
- `yaams doctor` is now also exposed as a subcommand for parity with
  `hugr doctor`. The `--doctor` flag still works.

## [0.1.5] - 2026-05-12

### Added

- `yaams sources` TUI now discovers all available calendar and teams
  profiles by shelling out to `owa-cal profiles` and
  `owa-piggy profiles --json`. Each profile shows as a child row and can
  be toggled in or out of `ingest.<source>.profiles` with space — no need
  to pre-list profile names in config. Discovered profiles are tagged
  (`default`, `webcal`); profiles in config but not discovered are
  marked `not discovered`. Discovery fails soft if the CLIs are missing.
- Per-entry `enabled: true/false` for `folders.paths` and `email.sources`.
  Toggling a child row in the TUI flips just that entry's flag without
  removing it. Bare-string folder entries get rewritten to dict form on
  first disable; defaults remain enabled. Ingest skips disabled entries.
- `yaams ingest` now prints a column-aligned per-source table with a
  TOTAL row, plus a separate `Total new items ingested` summary line.

## [0.1.4] - 2026-05-12

### Added

- New `folders` ingest adapter for generic recursive folder ingestion.
  Reads `.txt` and `.md` natively; `.pdf` requires `pypdf`, `.docx`
  requires `python-docx`. Unsupported types are skipped. Configurable
  `extensions` and `skip_dirs`. Wired through `yaams ingest --source folders`
  and included in `--source all`.
- `yaams sources` TUI now manages path-list sources inline: press `a` to
  add a path and `d` to remove one for `folders` and `email`. The
  `folders` row is always shown, so the first path can be added before
  any `ingest.folders` block exists in config; the block is created on
  first add.
- `config.yaml.example` documents the new `ingest.folders` block.

## [0.1.3] - 2026-05-12

### Fixed

- `setup`, `reset-db`, and `ingest` now wrap `load_config()` in a
  try/except that emits the standard action envelope (with
  `error.code=config_unreadable`) and exits `EXIT_USER_ERROR` when the
  config is missing or invalid, instead of leaking a raw Python
  traceback on stderr. Restores `--json` parseability for these
  commands.
- `yaams/cli/ingest.py` was missing `emit_action` in its conventions
  import; the error branch raised `NameError`. Import fixed.

## [0.1.2] - 2026-05-12

### Added

- `yaams sources` interactive TUI to enable/disable ingest sources. Arrow
  keys navigate, space toggles, enter applies. Stdlib-only (termios/tty);
  rewrites only the `enabled:` lines in `config.yaml` so comments and
  structure are preserved.
- File-based logging via `yaams/logsetup.py`. Ingest now writes to
  `<db_dir>/logs/yaams-YYYY-MM-DD.log`; `yaams ingest -v/--verbose` also
  streams DEBUG to stderr.
- GitHub adapter logs token-fetch timing, per-page request timing,
  rate-limit headers, the effective `since` cutoff, and total
  seen/yielded counts.
- `[project.optional-dependencies].dev` with `pytest`, `ruff`, `pyright`,
  plus `[tool.ruff]` and `[tool.pyright]` configuration in `pyproject.toml`.
- GitHub Actions CI (`.github/workflows/ci.yml`): ruff lint + pytest on
  Python 3.11/3.12, with a non-blocking pyright job.

### Changed

- GitHub adapter wraps `gh auth token` in a 15s timeout and surfaces a
  clearer error if the binary is missing, so a stuck `gh` can no longer
  hang ingest forever.

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

[Unreleased]: https://github.com/damsleth/YAAMS/compare/v0.1.11...HEAD
[0.1.11]: https://github.com/damsleth/YAAMS/compare/v0.1.10...v0.1.11
[0.1.1]: https://github.com/damsleth/YAAMS/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/damsleth/YAAMS/releases/tag/v0.1.0
