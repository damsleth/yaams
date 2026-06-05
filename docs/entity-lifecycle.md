# Entity Lifecycle and Curation Contracts

Developer-facing contract doc for the entity layer: where entities come
from, the states they move through, what each curation verb actually
does, and the invariants automation must respect. The user-facing
walkthrough is `docs/user-guide.md` §7; this doc is the layer below it.
Everything here describes **current behavior** — verified against the
code on 2026-06-05. File:line anchors drift; the named functions are
the stable reference.

## The two populations and the state machine

Every row in `entities` carries `pending_review`:

| Value | Meaning | Created by | Removed by |
|---|---|---|---|
| `0` | **Curated** — vetted, or seeded from the config dictionary | `seed_entities` for `source in {dictionary, manual}` (`store.py`, see `pending_review = 0 if source in ...`) | `entities remove` (dictionary entry; DB row persists) |
| `1` | **NER candidate** — auto-extracted, unvetted | NER tagging at ingest/retag | merge (as victim), `prune` (→2), `vacuum` (deleted if unreferenced) |
| `2` | **Denied** — junk, permanently blocked | `entities prune` (`store.py` `prune` function) | `entities denied` re-allow only |

Transitions worth stating explicitly:

- **1 → 0 happens only via the dictionary.** There is no "approve" verb
  on the DB row; an NER candidate becomes curated by being added to the
  config dictionary (`entities add`, `discover`, `import-people`, or as
  a merge survivor) and reseeded.
- **2 is sticky by design.** `prune` marks the row denied, strips its
  `item_entities` links and derived data, and removes any dictionary
  entry. Because the row remains with `pending_review = 2`, re-ingest
  (`INSERT OR IGNORE`) cannot revive the name. `vacuum` never touches
  0 or 2.
- **`vacuum` deletes only orphans**: `pending_review = 1` rows with no
  item links, tags, meta, relations, associations, or promotion
  candidates. It is reference-counting cleanup, not judgment.

## Birth: how entities get minted

Tagging (`yaams/enrich/entities.py`) runs two passes per item:

1. **Dictionary pass** — exact case-folded match against canonical
   names *and aliases* from the config dictionary. Hits get
   `confidence 1.0, source 'dictionary'`. Dictionary hits always win
   over the junk filters.
2. **NER pass** — spaCy entities mapped through a fixed label table:
   `PERSON`/`PER` → `person`, `ORG` → `org`, `GPE`/`LOC` → `place`.
   Everything else is dropped. Hits get `confidence 0.7, source 'ner'`,
   after sanitization (URL/markdown/HTML stripping) and the
   `NOISE_WORDS` junk filter.

Consequence: **NER can only ever mint `person`, `org`, or `place`.**
Any other `entity_type` in the DB is dictionary-origin by construction.

Known gap (tracked in `.plans/automation/alias-identity-invariant.md`):
the NER pass checks the dictionary for *tagging* but minting does not
check whether the surface form is already a curated entity's alias —
so "Kim" can exist as its own NER entity while also being an alias of
a curated person. Until the invariant lands, periodic merges are the
mitigation.

## The dictionary: config is the durability layer

The entity dictionary lives in `entities.json` next to the DB
(`yaams/entities_store.py`), loaded transparently into
`config["entities"]["dictionary"]`. The split matters:

- **The DB is disposable; the dictionary is not.** `reset-db` deletes
  the database only. Re-ingest + reseed reconstructs curated entities
  from the dictionary. Anything recorded only in the DB (NER
  candidates, denials, item links) does not survive a reset.
- `seed_entities` upserts dictionary entries into the DB as
  `pending_review = 0`; `backfill_entity_sources` upgrades existing
  `'ner'` links to `'dictionary'` for newly curated names. Both run in
  every `refresh`.

## The durable merge contract

A merge is **two-phase, in this order**:

1. **Dictionary fold** — victim canonical names *and all their aliases*
   are folded into the survivor's dictionary entry as aliases; victim
   dictionary entries are removed; the file is saved and the DB
   reseeded. (`_apply_merge`, `yaams/cli/entities.py` — shared by
   `entities merge` and the dedupe TUI.)
2. **DB repoint** — `store.merge_entities` repoints `item_entities`
   (max-confidence on conflict), `entity_tags` (insert-or-ignore),
   `entity_meta` (**survivor's value wins per key** — it is one value
   per `(entity_id, key)`), `entity_relations` (dedupe, drop
   self-loops), `promotion_candidates` (by canonical name); deletes the
   victim rows; and **drops the victims' `entity_assoc` rows** (run
   `assoc build` afterwards).

**Phase 2 without phase 1 is not durable.** The
`store.merge_entities` docstring states the contract: the caller must
fold config first, or the next NER re-tag re-mints the victims and the
merge silently un-does itself. Any new code path that merges (TUIs,
automation, reconcile verbs) must go through the full two-phase path —
treat `store.merge_entities` as a private primitive of it.

The one sanctioned exception is `entities normalize`
(`store.normalize_entities`): it auto-merges *edge-punctuation-only*
variants (`Hamas` / `Hamas'`) as a **pure DB cleanup with no config
promotion**. That is safe because NER canonical normalization already
emits the clean form, so the dirty variant cannot be re-minted. It
skips denied entities.

## Entity types: taxonomy and trust

Observed type vocabulary (live dictionary census, 2026-06-05): `person`
(83), `org` (75), `place` (26), `software` (20), `didcode` (5),
`course` (3), `project` (2), `email`, `meeting`, `team`, `tech` — the
vocabulary is **open**: `entities add --type <anything>` works, and a
dictionary entry without a type defaults to `"other"`.

- **`didcode`** is a functional type: timesheet codes (e.g. `IGNORE`,
  `CC LUNCH`, `NC NOCOS`) used by the did/timesheet flows. The string
  `didcode` appears nowhere in this codebase — the type is purely a
  dictionary convention, which is exactly why it must be documented:
  nothing in code would warn you before merging one into a project.
  **Never merge a didcode into another type.** Curation playbook: use
  `assoc link` to relate a didcode to its project instead.
- **Trust NER types only as a hint.** Dictionary types are
  human-asserted and reliable. NER types are frequently wrong on real
  data — observed during the 2026-06-05 curation pass: a person typed
  `org` ("CJ", 62 links), persons typed `place` ("Milos", "Sri",
  "Christophers"). Type *agreement* between two NER entities is weak
  evidence; type *conflict* involving a dictionary entity is strong
  evidence.
- There is **no verb that changes `entity_type`** on an existing
  entity. `entities set type=person` writes an `entity_meta` attribute;
  the column is untouched. (CLI gap; the workaround is
  prune-and-re-add, which loses links.)

## Associations: what the scores mean

`assoc build` (`yaams/retrieve/associate.py`) recomputes
`entity_assoc` from `item_entities` co-occurrence:

- Score is **normalized PMI**: `npmi = pmi / -log(p_ab)`, clamped to
  [0, 1] — 1.0 means the pair only ever appears together, ~0 means
  independence.
- Floors: pairs need `cooccur ≥ 3` (`min_cooccur`) and
  `score ≥ 0.15` (`min_score`) to be stored. Defaults are CLI flags,
  not config.
- Rows are stored **bidirectionally**; denied entities are excluded.
- Rebuild is **full delete + reinsert** — it is cheap and there is no
  incremental path. Merges drop victims' rows, so the table is stale
  after any merge pass until the next `assoc build` (`refresh` runs it
  unless `--skip-assoc`).
- `entity_relations` is the **manual layer** on top (`assoc
  link`/`suppress`/`unlink`): kind + weight + suppress flag, merged
  with learned rows at read time. Suppression blocks an edge whatever
  its learned score.

## Unattended-safe maintenance: the admission rule

`refresh` runs `_safe_maintenance` (`yaams/cli/workflows.py`) without a
human: `seed_entities`, `backfill_entity_sources`, `normalize`,
`vacuum`, then `assoc build`. What admits an action into this set:

1. **Unambiguous by construction** — no judgment call exists (normalize
   merges punctuation-only variants; vacuum deletes provably
   unreferenced rows).
2. **Curated and denied entities are never modified.**
3. **Idempotent and safe to rerun** — same guarantees as migrations.

Anything that picks between alternatives (which survivor, is this
junk, are these the same person) does **not** qualify today. The
`.plans/automation/` track proposes admitting classified suggester
output behind a replay-precision gate — that is a *plan*, not current
behavior; this section should be updated when it lands.

## Sharp edges summary (current state)

- Raw `store.merge_entities` is not a durable merge (see contract
  above).
- NER minting does not consult the alias table → shadow duplicates of
  curated entities accumulate.
- No verb changes `entity_type`; `entities set type=` silently writes
  meta instead.
- `entity_meta` is single-valued per key, survivor-wins on merge —
  do not store per-victim facts in it.
- `suggest-merges` survivor choice is by item count; `suggest-prune`
  hides high-link junk by design (`--max-items`, least-used-first).
  Both are advisory; review before applying.
- `items.lang` has existed since Phase A but is only populated for
  items ingested ≥ 0.4.0 (or after `backfill-lang`); treat NULL as
  unknown, not English.
