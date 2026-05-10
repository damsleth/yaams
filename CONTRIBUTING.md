# Contributing to YAAMS

Thanks for the interest. This document covers everything a contributor needs
that the user-facing [README](README.md) deliberately omits: development
setup, the test suite, schema-migration rules, commit conventions, and where
the design docs live.

If you are looking for *how to use* YAAMS, you want the README.

## Development setup

Requires Python 3.11+. The project lives in a single editable virtualenv.

```bash
git clone https://github.com/damsleth/YAAMS.git
cd YAAMS
python3.12 -m venv .venv          # or python3.11+
source .venv/bin/activate
pip install -e '.[dev]'
python -m spacy download xx_ent_wiki_sm
```

If you do not have a `dev` extra yet, install the runtime deps and pytest:

```bash
pip install -e .
pip install pytest
```

On Apple Silicon, use the Homebrew arm64 Python explicitly - PyTorch 2.4+
has no x86_64 macOS wheels:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
```

For a real ingest run against your own data, also:

```bash
cp config.yaml.example config.yaml
python scripts/configure_phase_a.py --config config.yaml
```

`config.yaml` is gitignored on purpose - it carries your entity dictionary
and source paths. Edit `config.yaml.example` instead when contributing
structural changes upstream.

## Running the tests

```bash
pytest -q
```

The full suite runs in under two seconds because everything uses synthetic
in-memory fixtures - see [docs/fixtures.md](docs/fixtures.md) for the
contract. **Never commit real messages, emails, or database fragments.**

To run a single test file:

```bash
pytest tests/test_imessage.py -v
```

To check coverage of a specific module:

```bash
pytest --cov=yaams.enrich --cov-report=term-missing
```

## Architecture

The high-level engine is `parse -> route -> retrieve -> fuse -> answer ->
log signals`. The full design lives in `.plans/yaams_architecture.md` and
`.plans/yaams_phase_a_ingest.md` (the planning directory is gitignored;
agents working in-tree should consult it).

Read [AGENTS.md](AGENTS.md) before touching code. It is the authoritative
entry point for any AI agent or human working in this repo.

Layout:

```
yaams/
  cli.py              # Click entry point
  config.py           # config.yaml resolution
  db.py               # SQLite open / sqlite-vec loading
  schema.py           # CREATE TABLE + idempotent migrations
  store.py            # writes: items, consolidations, entity links
  watermark.py        # per-source ingest cursors
  time.py             # timezone helpers
  ingest/             # one file per source adapter
  enrich/             # entity tagging, embedding
  retrieve/           # parse, route, hybrid query, fusion
  synthesize/         # LLM adapter contract + grounded synthesis
  signals/            # query and feedback logging
  promote/            # Tier 2 promotion candidates
  consolidate/        # LightMem-style session grouping
```

## Adding an ingest adapter

Use [docs/adapter-checklist.md](docs/adapter-checklist.md) as the spec. The
short version:

- Deterministic `source_id` per item; `hash_id(source, source_id)` for IDs.
- UTC timezone-aware timestamps; respect `ingest.since` and watermarks.
- Read source databases read-only (or against a copied file).
- Normalize to plain text; skip empty content.
- Dry-run path must avoid writes and heavy model loading.
- Synthetic fixtures only - cover idempotency, cutoff, timezones,
  source-specific threading hints.

Wire the adapter up in `yaams/cli.py` (the `ingest` command's
`_sources_to_run` and `get_adapter`) and add a `tests/test_<source>.py`.

## Schema migrations

Schema changes go in `yaams/schema.py`. Rules:

- `init_schema()` must be idempotent: it is called on every `init-db` and at
  the start of every ingest.
- Add new tables / columns through `_migrate_*` helpers that introspect
  current state with `PRAGMA table_info` and only `ALTER` when the column
  is missing.
- Bump `SCHEMA_VERSION` if the change is not reversible by re-running an
  ingest (e.g. dropping or re-typing a column).
- Document the change in [CHANGELOG.md](CHANGELOG.md) under the unreleased
  section.

The detailed migration policy lives in
[docs/schema-migrations.md](docs/schema-migrations.md).

## LLM backends

Synthesis runs through a pluggable adapter. The contract lives in
[docs/llm-adapter-contract.md](docs/llm-adapter-contract.md). Backends
must:

- Run with explicit timeouts.
- Accept argv as a list (never `shell=True`).
- Return a structured `LLMResponse` so latency and tokens can be logged.
- Surface errors as a string field on the response, not as raised
  exceptions, so partial answers can still be cited.

Adding a backend = a new class in `yaams/synthesize/llm.py` + a `case` in
`llm_adapter_from_config`.

## Commit conventions

Conventional Commits, lowercase scope:

```
feat(ingest): add Signal Desktop adapter
fix(embed): prompt to download model when HF cache is missing
docs(security): document GitHub Security Advisories flow
chore(deps): bump sentence-transformers to >=3.0
```

Scopes follow the package layout: `ingest`, `enrich`, `retrieve`,
`synthesize`, `signals`, `promote`, `consolidate`, `schema`, `cli`,
`config`, `docs`, `security`, `deps`, `tests`.

One logical change per commit. Keep commit messages explanatory - the *why*,
not the *what*. The diff already shows the what.

## Pull requests

- Branch from `main`.
- Run `pytest -q` before opening the PR.
- Update [CHANGELOG.md](CHANGELOG.md) under `## [Unreleased]`.
- If you touched the public CLI, update the README CLI table.
- If you added a schema migration, note it in the PR description.
- For new ingest adapters, include synthetic fixture coverage.

## Releasing

The maintainer cuts releases. The flow:

1. Move `## [Unreleased]` entries under a new `## [x.y.z] - YYYY-MM-DD`
   header in `CHANGELOG.md`.
2. Bump `__version__` in `yaams/__init__.py` and `version` in
   `pyproject.toml` (must match).
3. Commit as `chore(release): vx.y.z`.
4. Tag `git tag -a vx.y.z -m "vx.y.z"` and push tags.
5. GitHub release notes are generated from the changelog entry.

Pre-`1.0` rules: any breaking change (CLI surface, on-disk schema in a way
that requires manual migration, config file shape) bumps the **minor**.
Bug fixes and additive changes bump the **patch**.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not open public issues for
vulnerabilities - use GitHub Security Advisories.
