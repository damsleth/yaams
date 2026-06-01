# YAAMS User Guide

A practical, end-to-end manual for running YAAMS on your own machine: what
every feature does, how the pieces fit together, and how to get the most out
of it day to day.

This guide assumes YAAMS is installed and you have a working `config.yaml`.
If not, start with the [Quickstart in the README](../README.md#quickstart),
then come back here.

---

## Contents

1. [The mental model](#1-the-mental-model)
2. [The lifecycle](#2-the-lifecycle)
3. [Configuration](#3-configuration)
4. [Ingesting your data](#4-ingesting-your-data)
5. [Querying](#5-querying)
6. [Synthesized answers](#6-synthesized-answers)
7. [Entities: the curation workflow](#7-entities-the-curation-workflow)
8. [Associations](#8-associations)
9. [Consolidation](#9-consolidation)
10. [The relevance loop: signals, feedback, review](#10-the-relevance-loop)
11. [Promotion to Tier 2](#11-promotion-to-tier-2)
12. [Health checks and maintenance](#12-health-checks-and-maintenance)
13. [Best practices](#13-best-practices)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. The mental model

YAAMS is **Tier 1 of a two-tier memory architecture: the firehose.** It
ingests everything — messages, mail, calendar, GitHub, notes — normalizes it
into one schema, embeds it, and lets you search and synthesize across the
lot. [cognitive-ledger](https://github.com/damsleth/cognitive-ledger) is
Tier 2: the small set of curated atomic notes you keep forever. YAAMS feeds
Tier 2 through **promotion** (section 11).

Three principles shape everything else:

- **Local-first.** Embeddings (BGE-M3), NER (spaCy), and LLM synthesis all
  run on your machine by default. Nothing leaves the host unless you point an
  adapter at a hosted backend.
- **Append-only and idempotent.** Raw items are immutable. Item IDs are
  `sha256(source:source_id)`, so re-ingesting the same data is a no-op — you
  can run `ingest` as often as you like.
- **Single SQLite file.** Your entire store is one file at `db_path`. No
  daemon, no server. Back it up by copying the file.

Everything you store is sensitive. The database is **not** encrypted by
YAAMS — turn on full-disk encryption (FileVault) on the host. See
[privacy-security.md](privacy-security.md) and [SECURITY.md](../SECURITY.md).

---

## 2. The lifecycle

```
ingest -> normalize -> embed -> retrieve -> fuse -> answer (cited)
```

In day-to-day terms:

1. **`ingest`** pulls new items from each enabled source (incremental, via
   watermarks), normalizes them, embeds them, and tags entities.
2. **`query`** does hybrid retrieval (full-text + vector) and fuses the
   results, optionally synthesizing a cited answer.
3. **`entities` / `assoc`** let you curate the entity graph so retrieval gets
   sharper over time.
4. **`consolidate`** groups chatty items into sessions so a single
   conversation surfaces as one result.
5. **`feedback` / `review`** capture relevance signals to measure and improve
   retrieval quality.
6. **`promote`** lifts the gems into your Tier 2 ledger.

A typical rhythm: ingest runs nightly and unattended; you query ad hoc; you
curate entities occasionally; you promote when you have something worth
keeping.

---

## 3. Configuration

`config.yaml` is resolved, in order, from `$YAAMS_CONFIG`, then
`~/.config/yaams/config.yaml`, then the repo root. It is gitignored — it
carries your data paths, entity dictionary, and addresses.

Bootstrap a default config:

```bash
yaams init                 # writes ~/.config/yaams/config.yaml
```

Edit any source from the interactive TUI instead of hand-editing YAML:

```bash
yaams sources              # toggle sources on/off, set paths/profiles
```

### Key blocks

```yaml
db_path: ~/yaams/data.db

ingest:
  since: '2025-01-01T00:00:00Z'   # global lower bound for first ingest
  imessage:
    enabled: true
    chat_db_path: ~/Library/Messages/chat.db
  email:
    enabled: true
    sources:
      - type: emlx
        path: ~/Library/Mail/V10   # point at the newest ~/Library/Mail/Vxx
    skip_newsletters: true
    user_addresses: [you@example.com]   # never filtered as automated mail

embed:
  model: BAAI/bge-m3
  device: mps                # mps (Apple Silicon) | cpu | cuda
  dimension: 1024
  offline: true              # set false once when changing model, to download

synth:
  backend: dummy             # claude | codex | ollama | subprocess | dummy

entities:
  spacy_model: xx_ent_wiki_sm
  dictionary:
    - canonical: Example Org
      type: org
      aliases: [EX, ExampleOrg]
```

A few keys that matter more than they look:

- **`synth.backend`** powers *both* `query --answer` synthesis **and** the
  LLM query parser. With `dummy` (the default), you get no synthesized
  answers and the parser silently falls back to dumb text search — losing
  shape, entity, and association inference. See
  [Troubleshooting](#14-troubleshooting). Set a real backend to unlock the
  smarts.
- **`embed.offline: true`** means YAAMS only uses the locally cached model
  and never hits the network per run. Flip it to `false` the first time you
  change `embed.model`, then back to `true`.
- **`email.user_addresses` / `mail.user_addresses`** — list every address you
  send from. Mail *from* these is never dropped as a newsletter even if the
  subject looks automated.

The full annotated template lives in `yaams/_default_config.yaml` (or the
installed `config.yaml.example`).

---

## 4. Ingesting your data

```bash
yaams init-db              # create the schema (idempotent)
yaams ingest --dry-run     # see what each adapter would pick up
yaams ingest               # the real thing
yaams ingest --source imessage   # one source only
```

### Sources

| source | what it ingests |
| --- | --- |
| `imessage` | iMessage from the local `chat.db` (read-only copy) |
| `signal` | Signal Desktop (1:1 + groups, attachment metadata) |
| `email` | `.emlx` (Apple Mail) or `.mbox`, with quoted-reply trimming |
| `notes` | Obsidian vault markdown |
| `folders` | generic recursive file walker (`.txt`, `.md`, `.pdf`, `.docx`) |
| `tier2_ledger` | curated atomic notes from cognitive-ledger |
| `github` | issues and PRs across your repos |
| `calendar` / `calendar_<profile>` | Outlook calendar via `owa-cal` |
| `mail` / `mail_<profile>` | Microsoft 365 mail via `owa-mail` |
| `teams` / `teams_<profile>` | Microsoft Teams via Graph |

Microsoft 365 sources are **profile-keyed**: configure a profile once with
`owa-piggy setup --profile <name>` and reference it under `profiles:`. Each
profile becomes its own source id (e.g. `mail_work`, `calendar_work`), so you
can ingest and query per identity.

### Incremental and idempotent

Ingest is **incremental** — watermarks track how far each source has been
read, so a run only fetches what's new. It's also **idempotent**: a run that
re-sees only known items does no embedding work and finishes in a few
seconds. Run it as often as you want; there's no penalty for re-running.

The first real ingest downloads the embedding model (`BAAI/bge-m3`, ~2 GB)
and prompts before doing so. After that, ingest is fully offline.

Useful flags:

- `--strict` — treat any source failure as fatal (exit 1) instead of
  partial-success (exit 5). Good for scripts and CI.
- `--json` — NDJSON progress plus a final action envelope, for automation.
- `-v` / `--verbose` — stream DEBUG logs to stderr.

### Unattended scheduling

YAAMS is built to run nightly without you. See
[scheduling.md](scheduling.md) for a `launchd` agent and — importantly — the
**Full Disk Access** setup the `imessage` adapter needs to read `chat.db`
under `launchd`.

---

## 5. Querying

```bash
yaams query "what did we decide about the deploy in May"
```

Retrieval is **hybrid**: full-text (FTS5) and dense vector search, fused into
one ranked list. By default an LLM parser interprets your query first
(detecting time windows, entities, and intent) — provided you've configured a
real `synth.backend`.

### Scope and filtering

```bash
yaams query --top-k 20 "deploy"
yaams query --source imessage --source teams_work "holiday plans"
yaams query --tier ledger "principles I wrote down"      # only Tier 2 notes
yaams query --since 2026-01-01 --until 2026-03-01 "Q1 planning"
yaams query --no-consolidations "raw items only"
```

- `--source` (repeatable) restricts to specific sources. `--source ledger` is
  an alias for the internal `tier2_ledger`.
- `--tier raw|ledger|both` restricts by tier. Explicit `--source` wins over
  `--tier`.
- `--since` / `--until` take ISO timestamps.

### Sorting

```bash
yaams query --sort newest "latest from the vendor"
yaams query --sort oldest "how this thread started"
```

Default is relevance. The parser also infers sort from query *shape* (a
"latest X" question sorts newest-first); `--sort` overrides that inference.

### Speed vs. smarts

```bash
yaams query --no-vector "fast FTS-only path"   # skips embedder load entirely
yaams query --no-parse "literal text, no LLM interpretation"
yaams query --no-synonyms "don't expand aliases"
```

- `--no-vector` is the fastest path — no embedding model is loaded. Great for
  exact-string lookups.
- `--no-parse` skips the LLM query parser and does a raw text → hybrid
  retrieve.

### Entity-aware retrieval

These build on the entity graph (sections 7–8):

```bash
yaams query --assoc "fdep"               # also surface associated entities
yaams query --tag customer "open issues" # restrict to entities tagged customer
yaams query --meta sector=public "..."   # restrict by entity attribute
yaams query --tag customer --tag-mode boost "..."   # lift, don't restrict
```

- **`--no-synonyms`** disables alias expansion. By default a query for `nc`
  also matches `Norconsult` if that alias is registered.
- **`--assoc`** widens entity-filtered results to co-occurring entities,
  ranked below exact matches. Requires a resolved query entity and a built
  association table (`yaams assoc build`).
- **`--tag` / `--meta`** filter (or, with `--tag-mode boost`, just lift)
  results by entity membership tags and key/value attributes.

### Inspecting and scripting

```bash
yaams query --explain "..."     # print the parsed query JSON before results
yaams query --json "..."        # machine-readable output
yaams query --high-quality "..."# synthesis-grade depth (higher top_k)
```

`--explain` is your window into what the parser understood — use it whenever
results surprise you (see [Troubleshooting](#14-troubleshooting)).

---

## 6. Synthesized answers

```bash
yaams query --answer "what are the open items from the kickoff meeting"
```

`--answer` retrieves, then asks your configured LLM to write a grounded answer
with **citations back to the source items**. Every claim points at the items
it came from, so you can audit it.

This requires a real `synth.backend` in `config.yaml`. Options:

```yaml
synth:
  backend: ollama            # local Ollama server
  model: llama3.1
  host: http://localhost:11434
```

```yaml
synth:
  backend: subprocess        # any CLI that reads a prompt on stdin
  command: ["claude", "-p"]  # or ["codex", "exec", "--prompt-stdin"]
```

With `backend: dummy` (the default), `--answer` produces nothing useful and
the query parser runs in fallback mode.

---

## 7. Entities: the curation workflow

Entities are the proper nouns YAAMS extracts from your data — people, orgs,
projects. They power synonym expansion, tag/meta filtering, associations, and
promotion clustering. There are two populations:

- **Curated** entities (`pending_review = 0`) — ones you've vetted, plus your
  config `dictionary`.
- **NER candidates** (`pending_review = 1`) — auto-extracted, unvetted. These
  are where the noise lives.

The curation tools turn the messy NER population into a clean graph.

### See what you have

```bash
yaams entities list                  # all entities with item hit counts
yaams entities show "Crayon"         # type, aliases, tags, attributes, count
```

### Discover and add

```bash
yaams entities discover              # interactive: review NER candidates
yaams entities discover --min-count 10
yaams entities add "Jan Henning Peters" --type person --alias JHP --alias Jan
```

`discover` scans NER-tagged items and proposes new dictionary entries,
skipping obvious noise (stopwords, fragments). `add` seeds the DB
immediately and writes the entity to your config dictionary so it survives
re-ingest.

### Merge duplicates

The same real-world entity often shows up under several surface forms.

```bash
yaams entities suggest-merges        # groups that collapse to the same key
yaams entities merge "Crayon" "Crayon AS" "Crayon Group"   # survivor first
yaams entities dedupe                # interactive TUI to review suggestions
```

`merge` folds the victims' names and aliases into the survivor, repoints all
links/tags/attributes/relations, deletes the victims, **and records the victim
names as survivor aliases in your config** — so the merge is durable and
future re-tagging resolves them automatically.

### Normalize punctuation variants

```bash
yaams entities normalize --dry-run   # preview
yaams entities normalize             # auto-merge
```

`normalize` auto-merges entities that differ only by edge
punctuation/whitespace (e.g. `Hamas` / `Hamas'`, `` `Saksnavn` `` / `Saksnavn`).
These are unambiguously the same, so no review is needed.

### Prune junk

NER mis-tags common words and fragments as entities. The junk detector finds
them; you review and remove.

```bash
yaams entities suggest-prune              # list likely-junk candidates
yaams entities suggest-prune --max-items 5  # focus on low-traffic junk
yaams entities prune "takk" "ja" "2024"   # actually remove them
```

`suggest-prune` is **read-only and advisory** — it never deletes anything. It
flags NER candidates (never curated ones) with reasons: `stopword`,
`all-lowercase`, `very-short`, `numeric`, `symbol-heavy`. It sorts
least-used-first (safest to prune) and prints a ready-to-paste `prune`
command. `prune` is the destructive step: it marks the entities denied,
strips their links and derived data, and removes them from the config
dictionary so re-ingest can't revive them.

### Add structure with tags and attributes

```bash
yaams entities tag "Crayon" customer defense-sector
yaams entities set "Crayon" sector=public region=oslo
yaams entities untag "Crayon" defense-sector
yaams entities unset "Crayon" region
```

Tags and attributes drive `query --tag` and `query --meta`. Use tags for
membership ("this is a customer") and attributes for key/value facts.

### One-stop interactive manager

```bash
yaams entities manage      # curses TUI for the whole dictionary
```

### Recommended curation pass

A good periodic cleanup, safest first:

```bash
yaams entities normalize          # 1. auto-merge punctuation variants
yaams entities suggest-merges     # 2. review and merge real duplicates
yaams entities suggest-prune      # 3. review junk, then prune
yaams entities discover           # 4. promote good NER candidates
```

---

## 8. Associations

Associations capture which entities tend to show up together — learned from
co-occurrence, plus manual links you assert. They power `query --assoc`.

```bash
yaams assoc build                          # recompute the learned table
yaams assoc show "Crayon"                  # associated entities (learned + manual)
yaams assoc link "fdep" "langkaia"         # assert a manual relation
yaams assoc suppress "fdep" "noise-entity" # block a (learned or manual) edge
yaams assoc unlink "fdep" "langkaia"       # remove a manual relation
```

Run `assoc build` after a substantial ingest or entity-merge pass so the
learned co-occurrence table reflects current data. Then `query --assoc`
widens a search about one entity to its associates, ranked below exact hits.

---

## 9. Consolidation

Conversations are chatty: one decision might span thirty iMessages.
Consolidation groups conversational items into **sessions**, so a single
conversation surfaces as one result instead of thirty fragments.

```bash
yaams consolidate                  # group items into sessions
yaams consolidate --source imessage
yaams consolidate --rebuild        # clear existing sessions and regroup
yaams consolidate --dry-run
```

By default `query` searches consolidations alongside raw items. Use
`query --no-consolidations` to search raw items only. Re-run `consolidate`
after large ingests to fold the new items into sessions.

---

## 10. The relevance loop

YAAMS can measure and improve its own retrieval quality from your feedback.

### Signals

Every query is logged by default (opt out per query with `query --no-log`).

```bash
yaams signals                # recent query history
yaams signals --limit 50
```

### Feedback

Tell YAAMS whether a result was good:

```bash
yaams feedback <query_id> hit
yaams feedback <query_id> miss --message "expected the May thread"
yaams feedback <query_id> correction --result <result_id> -m "this one, not that"
yaams feedback <query_id> noise          # mark the query itself as junk
```

When `query` runs in a terminal it also prompts for a quick verdict
(`h`/`m`/`1-9`/`n`) right after showing results — the fastest way to build up
signal. Turn it off with `query --no-prompt`.

### Review

Walk the backlog of unjudged queries and label them:

```bash
yaams review              # interactive curses TUI over the unjudged queue
yaams review --queue      # dump the prioritized queue as text
yaams review --stats      # dashboard: hit/miss rates over time
yaams review --json       # machine output
```

This is the loop that lets you see, over time, whether your curation and
config changes are actually making retrieval better.

---

## 11. Promotion to Tier 2

When the firehose surfaces something worth keeping forever, promote it into
your [cognitive-ledger](https://github.com/damsleth/cognitive-ledger) inbox.

```bash
yaams promote generate            # scan recent items for candidates
yaams promote generate --days 60
yaams promote list                # see the queue
yaams promote review              # interactive: accept / edit / reject / skip
```

**Nothing is promoted without your explicit acceptance.** Accepted notes land
in `promote.inbox_path` (default `~/yaams/ledger-inbox/`), ready for you to
file into the ledger proper.

---

## 12. Health checks and maintenance

```bash
yaams doctor                # environment + config health check
yaams doctor --json         # machine-readable
yaams stats                 # item counts per source + last ingest timing
yaams version               # version (--json for machine output)
yaams setup                 # install runtime assets (spaCy NER models)
```

Re-tag stored items after changing your entity dictionary or NER model:

```bash
yaams enrich retag          # re-tag all stored items with current models/dict
```

Destructive reset (asks first):

```bash
yaams reset-db              # drop and recreate the database
```

**Backups:** your entire store is the single file at `db_path`. Copy it
somewhere safe periodically — and keep the host's full-disk encryption on.

---

## 13. Best practices

- **Configure a real `synth.backend` early.** It unlocks both synthesized
  answers and the smart query parser. On `dummy`, you're running a much
  dumber YAAMS than the one you installed.
- **Let ingest run nightly and unattended.** It's incremental and idempotent,
  so there's no cost to frequent runs and no risk of duplicates. Set up the
  `launchd` agent ([scheduling.md](scheduling.md)).
- **Curate entities periodically, safest-first:** `normalize` →
  `suggest-merges`/`merge` → `suggest-prune`/`prune` → `discover`. A clean
  entity graph makes synonym expansion, tagging, and associations pay off.
- **Rebuild associations after big changes.** `assoc build` after a large
  ingest or merge pass keeps `--assoc` queries accurate.
- **Re-consolidate after large ingests** so new conversational items fold
  into sessions.
- **Use the fast path when you know it's exact.** `query --no-vector` skips
  the embedder load entirely for literal-string lookups.
- **Reach for `--explain` whenever results surprise you.** It shows exactly
  what the parser understood — the fastest way to diagnose a bad query.
- **Feed the relevance loop.** Answer the post-query verdict prompt; run
  `yaams review` occasionally. Over time `review --stats` tells you whether
  things are improving.
- **Tag your important entities.** A little `entities tag` / `entities set`
  effort makes `--tag` / `--meta` filtering powerful.
- **Promote deliberately.** YAAMS is the firehose; the ledger is forever.
  Only the gems should move up.

---

## 14. Troubleshooting

**Queries feel "dumb" — no time windows, no entity awareness.**
The query parser falls back to plain text search **silently** when there's no
working LLM backend. Check two things: (1) `synth.backend` is set to a real
backend (not `dummy`) and that backend actually runs; (2) run
`yaams query --explain "..."` — if the parsed JSON is empty or trivial, the
parser fell back. Shape/entity/association inference all depend on a live
backend.

**`--answer` returns nothing useful.**
Same root cause: `synth.backend` is `dummy` or misconfigured. Configure
`ollama`, `subprocess`, etc. (section 6).

**First ingest is slow / wants to download ~2 GB.**
That's the BGE-M3 embedding model downloading once. YAAMS prompts before
downloading. After the cache is populated, set `embed.offline: true` and runs
are fully offline. A no-op re-ingest (only known items) takes a few seconds.

**`imessage` ingest works manually but not under `launchd`.**
The `launchd` process needs **Full Disk Access** to read `chat.db`. See the
setup steps in [scheduling.md](scheduling.md).

**Changed `embed.model` and embeddings look wrong.**
Set `embed.offline: false` once so the new weights download, run an ingest or
`enrich retag`, then set it back to `true`.

**Entity dictionary changes aren't reflected in old items.**
Existing items keep their tags from ingest time. Run `yaams enrich retag` to
re-tag everything with the current dictionary and NER model.

**Too many junk entities.**
Run `yaams entities suggest-prune` (read-only) to see the worst offenders with
reasons, then `yaams entities prune ...` to remove them durably.

---

For the architecture and what's implemented, see
[implementation-status.md](implementation-status.md). For privacy and the
threat model, see [privacy-security.md](privacy-security.md) and
[SECURITY.md](../SECURITY.md).
