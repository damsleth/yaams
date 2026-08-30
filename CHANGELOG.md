# Changelog

All notable changes to YAAMS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-`1.0` releases may include breaking changes to the on-disk schema and CLI
surface; pin to a specific version if you need stability.

## [Unreleased]

### Added
- **The autoresearch loop grew a persistent knowledge wiki** (WikiSkill,
  arXiv 2608.27454) at `docs/experiments/wiki/`. The loop already had raw
  traces (results/campaign TSVs, experiments.jsonl), an evolvable skill
  surface (`yaams/retrieve/*`), and validation gating; what it lost between
  campaigns was consolidated knowledge and the diffs of rejected proposals.
  Now `patterns.md` carries cross-experiment patterns (seeded by
  consolidating the jun-aug 2026 campaign history), `proposals/` preserves
  every proposal's full diff and verdict - rejected ones included - via
  `docs/experiments/wiki.py`, and `evolution.md` indexes them. The workflow
  (`scripts/autoresearch_loop.workflow.js`) gained a per-round wiki
  maintainer step that consolidates traces into patterns, its planner,
  experiment, and critic agents now read the wiki first, and its recorder
  preserves every experiment's diff. The wiki persists across rejected skill
  updates, so knowledge compounds even in dry rounds.

### Changed
- **Ingest went from 4m20s to ~22s, almost all of it `teams_channels`.** Three
  fixes, each found by measuring rather than reasoning. `owa-teams messages`
  had grown a `--since` flag while the adapter's docstring still said it
  hadn't, so every run pulled `limit_pages` of history per channel and threw
  away everything before the watermark; it now stops paging at the cutoff and
  `limit_pages` is just a safety cap. The per-team `channels` and per-channel
  `messages` calls are subprocess waits, so they now run in a thread pool -
  one task per team doing its own channels-then-messages calls, deliberately
  not two `map` passes, which barriered every messages call behind the slowest
  channels call. And roughly one `owa-teams` call per run hangs and returns at
  ~61s, which alone was the difference between a 19s and a 1m32s ingest, so
  `_run` now caps a call at 20s and retries through its existing retry loop
  (counted in `timed_out_calls`). A companion owa-piggy fix - serving
  `token --json` from the access-token cache instead of round-tripping AAD on
  every call - cut the auth cost of each `owa-*` invocation from 0.31s to
  0.03s and took the cheap sources down with it (mail 1.1s -> 0.66s).

### Fixed
- **The post-ingest briefing could end with a chat-style question.** The
  summary is written one-way into the ledger inbox, but the model kept closing
  with "Vil du at jeg drafter svar...?" style offers to help. `build_prompt`
  now forbids it explicitly. Recovered from `worktree-no-question-summary`,
  where the fix had sat unmerged since 2026-07-08.
- **Consolidations recorded only the first 50 of their raw item ids.**
  `_to_consolidation` truncated `raw_item_ids` to 50 while `item_count` stayed
  the true count. The cap silently matched `DEFAULT_MAX_SESSION_ITEMS`, so it
  was invisible on the default config — but `max_session_items` is a supported
  key, and past 50 items the extras never got `consolidated_into` set. They
  came back from item search alongside the consolidation that already
  summarized them, were re-consolidated on the next run, and went missing from
  the entity-routing and lang-filter joins that read `raw_item_ids`.
- **Timestamp-sorted retrieval returned the weakest of tied results first.**
  The sort key `(timestamp, -score)` under `reverse=True` inverted the
  secondary key along with the primary. Same-minute chat messages and
  day-granularity document timestamps hit this constantly, so "what's the
  latest on X" surfaced the worst match of that timestamp.
- **Time-windowed queries lost any session that straddled the window edge.**
  Consolidation windowing required containment (`start >= since AND end <=
  until`), so a session running 23:00 Monday to 01:00 Tuesday matched neither
  day — and because the item arms filter on `consolidated_into IS NULL`, its
  member items were excluded too. Now overlap.
- **Date-windowed queries silently degraded to FTS-only.** `since`/`until` are
  applied after the vector KNN, exactly like the entity and participant
  filters, but only those two widened `fetch_k`. The vector arm ran at base `k`
  and usually returned nothing after filtering.
- **Entity normalization could resurrect a denied entity.**
  `normalize_entities` excluded denied rows from its grouping query but
  resolved the merge survivor with `resolve_entity_id`, which does not filter
  on `pending_review`. A punctuation variant produced by a later NER pass
  merged the live entity's links, tags and promotion candidates into the
  pruned row.
- **Promotion novelty was a cliff, not a gradient.** `admission_score` derives
  `novelty = 1 - dedup_similarity`, but `dedup_similarity` was only assigned
  inside the merge band, so a candidate at 0.79 similarity scored as fully
  novel — identical to one at 0.0.
- **`_is_covered` suppressed promotion on unrelated notes.** Bare substring
  containment meant entity "Ada" was judged already-covered by a note titled
  "adaptation plan", and "AI" by "email strategy". Now word-boundary matched.
- **Bots leaked into chatsvc recipients.** The Teams chatsvc adapter built its
  display-name roster in pass 1, before the bot and system gates in pass 2, so
  a bot posting in a group chat was dropped as a sender but still appeared in
  `recipients` of every human message in that chat.
- `promote generate`/`from-facts` now report the count of *newly stored*
  candidates rather than attempts (`store_candidates` counts `INSERT OR IGNORE`
  rowcount), so a re-run correctly reports `0 new`.

- **Ledger inbox notes were written in a format the ledger rejects.**
  `write_summary_to_inbox` used `datetime.isoformat()`, which emits `+00:00`;
  cogled's `sleep lint` requires a trailing `Z` and rejects the offset. Every
  ingest run therefore filed a note into `00_inbox/` that the ledger's own lint
  refused — 2 errors per run. The cogled-side seam contract
  (`docs/yaams-cogled-interface.md` §2c) had already specified `Z`, so this was
  YAAMS violating an existing contract rather than an underspecified one.
  Hand-fixing the files was cosmetic: the writer regenerated the error on the
  next ingest. Fixed at the producer; naive and tz-aware `when` now format
  identically.

### Changed
- `merge_entities` no longer returns `tags`/`meta`/`relations` counts. Nothing
  ever incremented them, so every caller reported a hard 0. `victims` and
  `item_links` are unchanged.
- Removed the `promote.conflict_detection.only_for_merge_band` config key. The
  guard it fed also required `merge_with is not None`, which is only ever set
  in the merge band, so setting it `false` did nothing. Configs still setting
  it are ignored, not rejected.

### Internal
- **Recency decay v2 was a void measurement, not a kill; recency at tau=90
  remains untested on the 79-gold era.** The variant scored raw Tier 1 items by
  `max(floor, exp(-age/tau))` with `tau=90d, floor=0.9`, so the floor binds for
  anything older than `90*ln(1/0.9)` = 9.49 days. The frozen fixture's *newest*
  item is `2026-06-30T20:30Z` and the run was 2026-08-21 — a minimum age of 52
  days (`exp(-52/90)` = 0.56). The factor was therefore exactly `0.9` for all
  70,134 items, and a uniform multiplier cannot reorder them. Because the decay
  skips `cfg.tier2_source`, the only ranking effect was a flat 10% demotion of
  raw Tier 1 against exempt `tier2_ledger` rows and consolidations: the 4
  regressions and the 0.6076 -> 0.5357 drop measure tier demotion, not age.
  No change shipped, and none was warranted — but the earlier conclusion drawn
  from it ("this corpus does not reward recency", "holds across two eras and
  two scopings") is withdrawn. It rests only on the jun18 `recency-f0.9` run on
  the ≈46-gold era. A retry needs a floor that does not bind across the
  fixture's age range, or `now` pinned to the fixture's max timestamp.
  Correction logged as `recency2-raw-t90-f0.9-void` (seq 86) against the
  original `recency2-raw-t90-f0.9` (seq 84), which stays as recorded — the
  timeline is append-only.
- **Both experiment branches are now published.**
  `autoresearch/retrieval-recency-v2` (`e0dbeab`, the recency decay v2 variant)
  and `worktree-wf_4f4cb97e-5bf-5` (`e48347b`, an unfinished
  `fts_rank_dominance_tiebreak` pass recovered from a stale worktree) were
  local-only — no remote ref, no upstream, and timeline rows carrying an empty
  `commit`, so a fresh clone could not reach either. Both are pushed and
  tracking. Their `.plans/*.md` design notes remain gitignored, so the branch
  code is now the record.
  Two warnings for anyone picking these up: `retrieval-recency-v2` defaults
  `recency_decay_tau_days` to `90.0` where the pre-registered plan specified
  `0.0` (disabled), so merging it as-is ships the degenerate decay on by
  default; and `worktree-wf_4f4cb97e-5bf-5` was never measured at all.
- Thread-coherence scoring now does one batched id lookup instead of a query
  per fused candidate, and the unbounded `IN (...)` lists in entity-allowlist
  resolution, association weighting and consolidation clearing are chunked
  under SQLite's 999-variable limit.
- `note_index.json` is parsed once per promote run instead of three times up
  front plus once per merge-band candidate.
- The Obsidian and chats ingest adapters share their markdown plumbing via
  `yaams/ingest/_markdown.py`; the three duplicated profile-row branches in the
  sources TUI collapse to one table-driven helper. Both verified
  behaviour-preserving.
- First tests for `yaams/ingest/teams_chatsvc.py`, which had none — including
  the `space`/`topic` conversation filter whose fix in 348e358 only tested the
  `teams_channels` half.
- Deleted dead code: `reset_reranker_cache`, `has_sqlite_vec`,
  `consolidation_metadata`, `FTS_CONS_WEIGHTS`, and `_stash_component`'s unused
  `is_item` parameter.

## [0.10.0] - 2026-07-08

### Added
- Retrieval flywheel — precision-with-use from real agent traffic
  (`.plans/retrieval-flywheel.md`). The MCP tools `yaams_query`/`yaams_answer`
  now log every query with `provenance="mcp"` and return a `query_id`, so agent
  traffic (the 95% of real retrieval) becomes signal: answer citations and human
  corrections are positive labels, and low-confidence/`gaps` answers form a
  coverage backlog. New opt-in `retrieve.feedback_boost` config flag (default
  off) lifts a result's rank by a capped count of the positive signals naming
  it; enable only after logged traffic accumulates and the frozen-fixture eval
  (`autoresearch_retrieval.py --feedback-boost`, leave-one-out) shows a gain.
- `yaams gaps [--provenance mcp]` — coverage backlog: questions answered poorly
  (low confidence, zero results, or reported gaps), grouped and ranked by
  frequency. The ingest to-do list from real usage.
- `yaams review --provenance mcp` — triage real agent queries in the
  scan-and-judge feedback TUI, not CLI/test rows.

## [0.9.0] - 2026-07-01

### Added

- **Local Outlook.app (macOS) ingest** — calendar and mail sources read via
  AppleScript against the desktop client (offline, no Graph). Detects "New
  Outlook" and explains the empty-result case instead of failing silently.
- **Agent-generated post-ingest summary** — each ingest run files a
  human-readable digest of what was pulled.
- **Autoresearch experiment timeline viewer** (`docs/experiments/`) — a
  self-contained, dependency-free dual-axis SVG chart of every measured
  retrieval experiment (kept, killed, re-baselined). Harnesses auto-log each
  recorded run so dead ideas are not re-tried.
- **Opt-in cross-encoder reranking** (`retrieve.rerank`, default disabled) over
  the hydrated candidate pool. The default retrieval path is byte-for-byte
  unchanged when disabled.
- **Advisory promote admission control** — `generate` computes and persists an
  `admission_score` (novelty / utility / confidence / trust) and the review UI
  shows the breakdown. Gating stays human-confirmed and never silently lossy.
- **Event-time bitemporal bridge** — promoted notes carry `valid_from` from
  source event-time when confident, and flag `valid_from_confidence: low`
  otherwise.
- **Norwegian↔English concept synonym group** (`isbad`) through
  `retrieve.synonyms`, plus the documented cross-lingual synonym path.

### Fixed

- Autoresearch rejudge-misses lane now re-parses query text fresh instead of
  reusing a stale parse.
- rerank-sweep baseline acceptance keys on MRR presence rather than run status.

### Internal

- ai-memory Track A research verdicts: reranking left **off by default**
  (pool-size sweep showed a flat curve); min-score admission gating **not
  adopted** as default (T7 eval; embedding dup-rate metric added to the ledger);
  raw-store append-only + `revision-in-source_id` invariant documented.
- Retrieval tuning: gold set densified to 79 labels; fusion/ordering knob space
  exhausted (0 generalizing wins); Norwegian FTS prefix and diacritics measured
  with no change shipped (prefix already optimal at ≥5, `remove_diacritics=0`
  kept).

## [0.8.0] - 2026-06-21

### Changed

- **Retrieval ranking — two structural gains** (autoresearch campaign on a
  densified 78-query gold set, dev quality 0.5329 → 0.6144, held-out test split
  neutral, 0 regressions):
  - `tier2_factual_coverage_recovery`: for factual-shape queries, an FTS-present
    but vector-absent tier2 ledger item (curated identity/decision facts) gets
    an additive RRF coverage credit before its tier2 boost, so single-modality
    facts stop losing the dual-coverage RRF race.
  - `thread_coherence_credit`: an atomic item whose `thread_id` matches a top-3
    consolidation's thread is lifted by a fraction of that consolidation's
    score — the focused member item rides the trust already earned by its
    parent session.

### Internal

- Autoresearch harness: replay at a fixed evaluation depth instead of each
  query's stored `top_k` (the latter truncated results before rank was measured,
  hiding correctly-retrieved golds).
- New `--rejudge-misses` lane in `llm_judge_unjudged.py` with a best-of-3
  adversarial verification pass and provenance-stamped feedback rows; used to
  densify the gold set from 68 to 78 labels.

## [0.7.1] - 2026-06-18

### Added

- **`chats` source** ("agent chats"): ingests Claude Code session summaries
  written by the `capture-chat.sh` `SessionEnd` hook — one markdown file per
  session, with YAML frontmatter. Reads `ingest.chats.chats_path` (default
  `~/brain/chats`), skips `.git`/`.obsidian`/`.claude` and `README.md`/
  `AGENTS.md`, and resolves timestamps from frontmatter (`created`/`date`/…) or
  the filename date, falling back to mtime. Disabled by default; enable under
  `ingest.chats` in `config.yaml`. Surfaced in docs as "agent chats" to
  disambiguate from messaging-chat sources (iMessage, Teams).

## [0.7.0] - 2026-06-17

### Added

- **MCP server** (`yaams mcp`): exposes Tier-1 query verbs as Model Context
  Protocol tools over stdio so any MCP client (Claude Desktop, Cursor, agents)
  can search the raw store directly — superseding the subprocess shim the
  cognitive-ledger MCP server used to wrap it. Tools: `yaams_query` (ranked
  results with trust verdicts), `yaams_answer` (grounded, cited synthesis), and
  the write-gated `yaams_feedback` (enabled with `--allow-write`). Every
  response passes through an egress scrub that strips `<private>…</private>`
  spans. Requires the optional `mcp` extra: `pip install 'yaams[mcp]'`.

- **Trust verdicts + provenance** (Tier-1 store): query results now carry a
  display-only trust verdict (`high|medium|low` + one-line reason) derived from
  the item's provenance class, affirming/contradicting feedback, supersession,
  and recency. Verdicts never affect ranking or result order. A new nullable
  `items.provenance` column (migration `0006_items_provenance`, schema v7)
  records each item's origin-trust class at ingest, derived from its source
  channel; legacy rows derive it at query time. New `trust:` config block:
  `show_trust_verdict` (default on) and `provenance_weighting_enabled` (default
  off until A/B validated). Ported from cognitive-ledger plans 42 & 46.

## [0.6.0] - 2026-06-10

### Added

- **`yaams promote commit`**: non-interactive verb to write promotion candidates
  to the Tier-2 ledger without a human at the keyboard. Supports `--candidate
  <id>` (repeatable), `--all`, `--min-score <float>`, and `--json`. Writes are
  idempotent (re-committing the same candidate is a no-op). Unblocks automated
  `ingest → promote` pipelines.

- **Schema migrations** (`yaams/migrations/`): numbered, journaled migration
  system replaces the old ad-hoc `_migrate_*` functions in `schema.py`.
  Migrations live in `yaams/migrations/versions/` as individual Python modules
  (`0001_baseline.py` … `0005_query_structured_fields.py`). Existing databases
  at `user_version=4` are automatically stamped on first open — no DDL re-runs.
  Adding a schema change is now one new file, zero edits to `schema.py`.

- **Review TUI speedup**: the scan-and-judge labeling interface is faster to
  use. Rank 1 expands on open; ranks 2–5 collapse to a one-line header (tab to
  expand). `enter` applies a heuristic default verdict (`hit` if query tokens
  appear in the snippet, `miss` otherwise). `?` defers a card to a later pass
  (`--deferred` flag surfaces it). Cards whose query has a `cited=1` result
  pre-populate with `hit` as the default, converting the cited signal into
  training labels retroactively.

- FTS prefix-star expansion: query terms ≥ 4 characters are expanded with `*`
  at search time, improving Norwegian morphology recall (øvelse → øvelse*
  matches øvelsen, øvelsene, etc.).

- `teams_channels` bot/automated post filter: new content-pattern filter drops
  Microsoft 365 message-centre posts (`Message ID: MC\d+`, `Published date:`,
  etc.). `_BOT_LIKE_NAMES` extended with `github`, `jira`, `azure devops`,
  `power automate`, `servicenow`, `jenkins`, `confluence`.

- `teams_channels.backfill_limit_pages` config key: one-time deep-backfill
  override for channels that were truncated at the default page limit.

- Autoresearch fixture DB now defaults to a stable path outside `/tmp` so it
  survives reboots.

### Changed

- Snippet length constants unified: `yaams.render.DEFAULT_SNIPPET_CHARS` is now
  the single source of truth; `signals.review._SNIPPET_LEN` and the CLI query
  body cap import from it.

- `reactions` and `reaksjoner` added to `NOISE_WORDS`; the teams reaction
  folding format changed to lowercase so `Reactions` is no longer tagged as an
  org entity (~300 spurious links removed on next retag).

- `teams_channels` steady-state: early-exit after page 1 when the newest item
  is at or behind the channel watermark, avoiding redundant page fetches on
  daily runs for up-to-date channels.

- Removed stale external-orchestrator references from planning notes.

## [0.5.0] - 2026-06-10

### Added

- Configured `retrieve.synonyms` concept groups for query-time FTS expansion,
  so Norwegian and English pairs like `vakt`/`shift` and `øvelse`/`exercise`
  can bridge zero-result lexical misses.

- **ConflictChecker** (`promote.conflict_detection`): optional LLM-based
  conflict classification for promotion candidates. Compares drafted candidates
  against the existing Tier-2 note they would merge into and routes
  contradictions to `_conflicts/` instead of the inbox. Off by default; enable
  after turning on dedup. Adds `conflict_*` fields to `PromotionCandidate` and
  a `promote.conflict_detection` config block (see `config.yaml.example`).

### Changed

- BM25 field weights tuned (`bm25w-s2`): subject field weighted 2× relative to
  `items_fts`, lifting `mrr_partial` from 0.323 → 0.338 on the dev gold set
  with zero regressions.

## [0.4.1] - 2026-06-09

### Fixed

- Ingest now skips owa-piggy-backed sources (mail, calendar, teams,
  teams_channels) whose profile has been deactivated in owa-piggy
  (`registered`/`scheduled` false), instead of attempting a fetch that fails
  with `invalid_grant`. Previously only teams and teams_channels were guarded;
  mail and calendar would still try the deactivated profile. Skipped profiles
  are logged (`skipping mail_<p>: owa-piggy profile '<p>' is deactivated`) and
  rejoin the ingest set automatically once re-activated.

## [0.4.0] - 2026-06-05

### Added

- `yaams refresh`: unattended routine workflow that runs ingest, then safe
  entity maintenance (dictionary seed/backfill and de-dupe, punctuation
  normalization, orphan vacuum) plus learned association rebuild. Supports
  `--skip-ingest`, `--skip-assoc`, `--dry-run`, and `--json`.
- `yaams curate`: human entity curation workflow. It runs safe maintenance,
  prints merge and prune suggestions, then enters interactive dedupe/discover
  only when attached to a real terminal.
- Language detection at ingest: every new item gets `items.lang` set to `"no"`
  or `"en"` based on the existing Norwegian heuristic (æ/ø/å characters plus
  function-word backstop). Items whose content is shorter than 10 characters
  are left as `NULL`. Existing stores can be backfilled with `yaams backfill-lang`.
- `yaams backfill-lang`: one-off command to populate `items.lang` for items
  ingested before this release. Processes rows in `id`-order batches so it
  always makes forward progress even when content is too short to detect.
- `yaams query --lang no|en`: hard-restricts retrieval to items (and
  consolidations whose constituent items) in the given language.

- Norwegian-aware NER: a second spaCy model (`entities.spacy_model_nb`, e.g.
  `nb_core_news_md`) handles Norwegian content. Items route to it when they
  contain æ/ø/å or at least two distinctly Norwegian function words; other
  content keeps using `entities.spacy_model`. Install with `yaams setup`.
- `yaams entities vacuum`: deletes unreviewed NER entities that nothing
  references anymore (no item links, tags, meta, relations, associations, or
  promotion candidates). These pile up when a re-tag with a better model or
  stricter filters stops linking old junk. Supports `--dry-run` and `--json`.
- Expression index `idx_entities_canonical_lower` over the Unicode-aware
  `lower()` (see Fixed), turning entity-name lookups from full scans into
  seeks; a full `enrich retag` dropped from ~8 to ~3.5 minutes on a 61k-item
  store.

### Changed

- The launchd template now runs `yaams refresh` nightly instead of raw
  `yaams ingest`, so scheduled runs also perform safe entity cleanup and
  rebuild associations.
- NER input is sanitized before tagging: markdown links/images keep their
  label but lose the target, raw URLs, e-mail addresses, and HTML tags are
  stripped. Entities containing markup residue (`://`, `](`, quotes, etc.)
  and 1-2 character fragments (except all-caps acronyms) are dropped.
- The `_NOISE_WORDS` junk list moved from the CLI to
  `yaams.enrich.entities.NOISE_WORDS` and is now applied **at tag time**, so
  known false positives (function words, greetings, e-mail header tokens like
  `cc`/`fwd`) never enter the entity table. Curated dictionary hits still
  always win. The CLI `discover`/`suggest-prune` junk detector reuses the
  same list.
- All-lowercase NER org canonicals are capitalized (`google` -> `Google`) so
  they fold into the properly-cased row instead of forking a duplicate.
- The mail subject fallback for newsletter/automated detection now covers
  Norwegian (`nyhetsbrev`, `avmeld`, `avregistrer`, `meld deg av`,
  `ikke svar`, `skal ikke besvares`) in the owa-mail path.
- The promotion draft prompt instructs the LLM to write title/statement/body
  in the dominant language of the sources (Norwegian or English) instead of
  drifting per draft; YAML keys and type values stay English.
- NER models now load only the `tok2vec` + `ner` pipes (the tagger, parser,
  lemmatizer etc. were dead weight since only `doc.ents` is consumed):
  identical entity output, ~1.3-1.4x tagging throughput.
- NER input normalization also folds exotic whitespace (nbsp, zero-width,
  bidi marks) to plain spaces and strips emoji/pictographs, both of which
  corrupt entity span boundaries ('Henrik\xa0Slettene', '🔹 Oppmøte').
- Docs now recommend `en_core_web_md` over `xx_ent_wiki_sm` as the fallback
  NER model for predominantly-English non-Norwegian content: on a 300-item
  bench of real chat/mail the multilingual model found zero PERSON entities,
  while `en_core_web_md` found 195. `xx_ent_wiki_sm` remains the shipped
  default (it is the safe choice for arbitrary-language content).

### Fixed

- `yaams entities discover` now treats an edited candidate as a merge into the
  saved canonical entity. The original NER row is deleted, existing item links
  are repointed and marked as dictionary-sourced, and the old surface is kept
  as an alias so the same candidate does not reappear on the next review pass.
- Entity de-duplication was ASCII-only: SQLite's built-in `lower()` does not
  fold non-ASCII, so `HØYRE`/`Høyre` (and every other æ/ø/å name) forked into
  separate entities. `db.open_db` now overrides SQL `lower()` with Python's
  Unicode-aware `str.lower`, fixing every entity-name comparison in
  store/retrieve/promote at once.

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
- The external conventions dependency was dropped in favour of an inline
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
- `yaams doctor` is now also exposed as a subcommand. The `--doctor`
  flag still works.

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
