# YAAMS: Vision, Architecture, and Roadmap

## The high-level goal

A single interface where you can ask any question about anything you've experienced or deliberately captured, and get a meaningful answer back. Two failure modes to avoid: hollow answers ("I don't have information on that") when the source material exists somewhere, and confidently wrong answers when the system stitches together unrelated material.

Concretely, the system should handle:

- "What did I tell Nina about the cabin last summer?" (entity + temporal + content)
- "When did I first hear about [topic]?" (temporal first-occurrence)
- "What's my position on [topic]?" (synthesis across sources, prefer ledger-curated)
- "Find that article I read about [thing]" (retrieval over ingested documents)
- "What did we decide at the SAR meeting in March?" (event-anchored)
- "Summarize my last 6 months of work on [project]" (longitudinal synthesis)

## Where we are now

**Cognitive ledger** (~199 notes, Python, hybrid semantic retrieval).

Curated atomic facts, concepts, identity, links, eval harness, A/B framework, signal loop, Electric Sheep consolidation. Phase 1 shipped `semantic_hybrid` retrieval (+24% hit1). Phase 2 hit a metric ceiling (87% single-relevant eval cases) and a corpus ceiling (154 notes too small for rerankers). Solid foundation, but the input is human-curated and the volume is small by design.

**Raw memory layer**: doesn't exist yet. Communications, emails, transcripts, reading history, browsing context, none of it is queryable.

## Where we want to get

A two-tier memory architecture:

**Tier 1: Raw memory.** Append-only ingestion of high-volume sources (messages, emails, transcripts, documents, browsing, screen capture if you want it). Embedded, entity-tagged, date-anchored. Queryable directly. High recall, lower precision.

**Tier 2: Curated ledger.** The existing cognitive ledger. Atomic, structured, human-validated. High precision, hand-curated coverage.

**Promotion pipeline.** Raw items get periodically scanned, consolidated into candidate atomic notes, surfaced for human review, promoted to the ledger when accepted. This is where LightMem-style sleep-time consolidation belongs.

**Unified query interface.** Single entry point. Routes queries across both tiers, fuses results, returns answers with sourcing across raw and curated material.

## Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Query Interface                          │
│  (parse → route → retrieve → fuse → answer → log signals)   │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
       ┌───────▼────────┐             ┌───────▼─────────┐
       │  Raw Memory    │             │ Cognitive Ledger│
       │  (Tier 1)      │             │  (Tier 2)       │
       │                │             │                 │
       │ - items table  │             │ - atomic notes  │
       │ - embeddings   │             │ - links/refs    │
       │ - entities     │             │ - identity      │
       │ - timeline     │             │ - signal loop   │
       └───────▲────────┘             └───────▲─────────┘
               │                              │
       ┌───────┴────────┐             ┌───────┴─────────┐
       │  Ingest Layer  │             │ Promotion Pipe  │
       │                │             │                 │
       │ messages,      │  ────────►  │ consolidate     │
       │ email,         │  candidate  │ → review        │
       │ transcripts,   │  entries    │ → promote       │
       │ docs, ...      │             │                 │
       └────────────────┘             └─────────────────┘
```

## Pipeline outline

### Ingestion

One adapter per source. Each adapter normalizes to a common `item` schema:

```yaml
id: <hash>
source: imessage | email | transcript | document | bookmark | ...
source_id: <native id from source>
timestamp: <ISO 8601>
sender: <person/account>
recipients: [<person>, ...]
content: <text>
content_compressed: <optional, for large items>
attachments: [<paths or blob refs>]
context: { thread_id, conversation_id, location, ... }
lang: no | en | mixed | ...
ingested_at: <ISO 8601>
```

Adapters run on a schedule (cron, launchd, or manual). Idempotent. Source watermarks track last-ingested timestamp per source.

### Processing

Three passes per item:

1. **Date-tag**: from source metadata. Cheap.
2. **Entity-tag**: NER + entity resolution against a known-entities dictionary (Nina, Emilie, Jacob, projects, places). Slower but local.
3. **Embed**: local embedding model, batched. The biggest compute cost.

Compression is conditional on content type. Conversational chitchat: aggressive (LightMem-style). Factual content (emails with dates/numbers/names): conservative or skipped. Already-curated material (ledger notes): never compressed.

### Storage

SQLite with extensions, single file. The list below is the canonical long-term schema. Each phase adds the subset it needs. Phase A implements `items`, `items_vec`, `items_fts`, `entities`, `item_entities`, and `watermarks` only. Item-schema fields like `content_compressed`, `attachments`, and `context`, plus the `timeline` and `consolidations` tables, arrive in later phases.

- `items` table: canonical item records (one row per ingested item)
- `embeddings` virtual table (sqlite-vec): vector index over item content
- `items_fts` (FTS5): keyword index for sparse retrieval
- `entities` table: known entities, aliases, types
- `item_entities` table: many-to-many (item_id, entity_id, confidence)
- `timeline` table: time-bucketed index for fast date-range queries (Phase D+)
- `consolidations` table: LightMem-style summary entries (Phase D+)
- `signals` table: query/result feedback, mirrors ledger schema (Phase B+)

Single-file SQLite scales fine to millions of rows for personal use. Add hot/cold partitioning later if items pass ~5M.

### Retrieval

Multi-stage, query-shape aware:

1. **Parse query**: extract entities, date ranges, query type (factual, summarization, exploratory).
2. **Filter**: entity and date filters narrow the candidate pool.
3. **Hybrid retrieval**: dense (embedding) + sparse (FTS5) over filtered pool. Fuse with reciprocal rank fusion.
4. **Cross-tier merge**: ledger results and raw memory results combined, with tier-aware scoring (ledger gets a small boost for curated content).
5. **Optional rerank**: cross-encoder pass for high-stakes queries. Skip for cheap ones.
6. **LLM synthesis**: top-N items passed to local LLM for answer generation, with explicit grounding requirement.

### Optimization loop

Same shape as the cognitive ledger's signal loop, extended:

- Capture per-query: retrieval results, final answer, latency, tokens.
- Capture per-feedback: hit/miss on specific items, answer rating, "I expected X" corrections.
- Periodic offline analysis (sleep-time): cluster failures by query shape, entity, date range, source. Identify systemic gaps.
- LLM-driven proposals: review failure clusters, propose config changes (entity dictionary additions, retrieval weight adjustments, missing source ingestion).
- A/B harness: any proposed change goes through the same eval framework as the ledger Phase 1/2 work.

### Promotion pipeline

Periodic (weekly?) job:

1. Scan recent raw items for patterns (repeated entities, recurring topics, decisions).
2. Cluster related items.
3. LLM generates candidate atomic notes from clusters.
4. Surface candidates in a review queue.
5. You accept, edit, or reject. Accepted candidates land in `notes/` and become ledger entries.
6. Original raw items get a `promoted_to: <ledger_id>` link for traceability.

Promotion is human-gated by design. Auto-promotion is the path to ledger pollution.

## Phased roadmap

### Phase A: First ingest source (1-2 weekends)

Just iMessage + email since a configurable cutoff date. End-to-end ingest pipeline: extract, embed, entity-tag, store. No query CLI, no compression, no consolidation, no promotion. Validation is via SQL spot checks against the database. Target: a populated SQLite store that Phase B can wrap a query interface around.

### Phase B: Query interface and feedback (1 weekend)

Wrap the storage with a proper query API. Build a CLI/TUI for asking questions, rating answers, capturing signals. Reuse cognitive ledger signal schema where possible.

### Phase C: Add sources (incremental)

One source at a time. Each one a separate adapter implementing the same item schema. Order by personal value: probably notes apps next, then transcripts, then browsing history, then documents. Ignore screen capture unless an obvious need emerges.

### Phase D: Consolidation (after Phase A has 2-3 months of data)

LightMem-style sleep-time consolidation pass. Reduces storage volume, improves retrieval quality on aged content, sets up promotion pipeline.

### Phase E: Promotion to ledger

Build the candidate generation, review queue, and promotion mechanics. Manual review at first. Automate the candidate generation, never the acceptance.

### Phase F: Cross-tier query fusion

Single query interface that hits both tiers and fuses. By the time this matters, you'll have enough data to know how to weight them.

### Phase G: Optimization loop

A/B framework, eval set construction (using lessons from ledger Phase 1/2), signal-driven config tuning. Don't build this until Phases A through F have produced enough data to optimize against.

## Design principles

- **Append-only by default.** Raw items are immutable. Edits become new items with `supersedes:` links.
- **Local-only compute.** Embeddings, NER, LLM inference all run in-environment. No external dependencies.
- **Idempotent ingestion.** Re-running an adapter against the same source produces the same items.
- **Source traceability.** Every consolidated entry, every promoted note, every answer points back to source items.
- **Human review at promotion.** Anything that lands in the ledger has been seen and accepted by you.
- **Feedback drives improvement.** Same signal-loop discipline as the ledger. No vibes-based optimization.

## What this isn't

- Not a continuous screen recorder. Capture is deliberate per source.
- Not a replacement for the ledger. The ledger remains the authoritative curated knowledge layer.
- Not a chat product. The interface is a query/answer/grade loop, not a conversational agent.
- Not a productivity tool. The output is recall, not action.

## Suggested next move

`yaams_phase_a_ingest.md` spells out Phase A: the iMessage and email ingest pipeline, end to end. Build that, run it against your last 12 months, see if the answers feel meaningful. If they do, the architecture is right and the rest is incremental work. If they don't, you'll know exactly where the gap is before committing to more sources.
