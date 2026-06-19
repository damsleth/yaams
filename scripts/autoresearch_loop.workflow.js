// Resume: just re-invoke this workflow — each run re-measures the anchor and
// re-reads the ledger/results.tsv/git from disk, so it continues the campaign
// from wherever the last run left it. Report progress any time with:
//   .venv/bin/python scripts/autoresearch_summary.py
export const meta = {
  name: 'autoresearch-loop',
  description: 'Autonomous retrieval-tuning loop: fan out Sonnet experiments, keep the best non-regressing win, iterate until dry',
  whenToUse: 'Run an unattended campaign of yaams/retrieve experiments against the frozen scenario',
  phases: [
    { title: 'Baseline', detail: 'measure current dev quality + p95 anchor' },
    { title: 'Round', detail: 'plan N ideas, fan out experiments, keep best win' },
    { title: 'Critic', detail: 'Opus re-reads corpus, seeds new ideas (periodic)' },
  ],
}

// --- knobs (override via args) ---
const FANOUT = (args && args.fanout) || 3            // experiments per round
const MAX_ROUNDS = (args && args.rounds) || 20       // hard cap
const CRITIC_EVERY = (args && args.criticEvery) || 5 // Opus critic cadence
const DRY_LIMIT = (args && args.dryLimit) || 2       // stop after N dry rounds
const HARNESS =
  '.venv/bin/python scripts/autoresearch_retrieval.py --split dev --json'
const PROGRAM = 'Read .plans/program-retrieve.md (the org code) and scripts/autoresearch_ideas.md (the ledger) first.'

const BASELINE = {
  type: 'object',
  properties: {
    quality: { type: 'number' },
    p95: { type: 'number' },
    rank1: { type: 'number' },
    gold: { type: 'number' },
  },
  required: ['quality', 'p95'],
}
const PLAN = {
  type: 'object',
  properties: {
    ideas: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          key: { type: 'string' },           // short slug, e.g. "rrf-k-resweep"
          idea: { type: 'string' },           // one-line hypothesis
        },
        required: ['key', 'idea'],
      },
    },
  },
  required: ['ideas'],
}
const EXPERIMENT = {
  type: 'object',
  properties: {
    key: { type: 'string' },
    status: { type: 'string' },               // ok | crash | fail:regression | fail:latency
    quality: { type: 'number' },
    p95: { type: 'number' },
    regressions: { type: 'number' },
    diff: { type: 'string' },                 // `git diff -- yaams/retrieve/` (empty if no change/discarded)
    note: { type: 'string' },
  },
  required: ['key', 'status', 'quality', 'regressions', 'diff'],
}

phase('Baseline')
const base = await agent(
  `${PROGRAM}\nRun the harness once to establish the anchor (no edits):\n` +
    `  ${HARNESS} --no-write\n` +
    `Return the quality, retrieval_p95_ms (as p95), rank1, and gold_queries (as gold) from its JSON.`,
  { label: 'baseline', phase: 'Baseline', schema: BASELINE },
)
let anchor = base.quality
let anchorP95 = base.p95 || 1e9
log(`anchor quality=${anchor} p95=${anchorP95}ms rank1=${base.rank1}/${base.gold}`)

let dry = 0
const kept = []

for (let round = 1; round <= MAX_ROUNDS && dry < DRY_LIMIT; round++) {
  if (budget.total && budget.remaining() < 60_000) {
    log(`stopping: budget remaining ${Math.round(budget.remaining() / 1000)}k < 60k`)
    break
  }
  const PH = `Round ${round}`
  phase(PH)

  // Planner: pick FANOUT distinct untried ideas from the ledger.
  const plan = await agent(
    `${PROGRAM}\nPick the ${FANOUT} highest-value DISTINCT untried Backlog ideas from the ledger ` +
      `that are in scope (ranking-only; editable surface = yaams/retrieve/* excluding parse.py). ` +
      `Skip anything marked discarded/parked. Return them as {key, idea}.`,
    { label: 'plan', phase: PH, schema: PLAN },
  )
  const ideas = (plan.ideas || []).slice(0, FANOUT)
  if (!ideas.length) {
    log(`round ${round}: planner found no untried ideas — dry`)
    dry++
    continue
  }

  // Fan out: each experiment in its own worktree, Sonnet.
  const results = await parallel(
    ideas.map((it) => () =>
      agent(
        `${PROGRAM}\nYou are running ONE experiment, autonomously, per the org code.\n` +
          `IDEA (${it.key}): ${it.idea}\n` +
          `Steps: (1) edit only yaams/retrieve/* (not parse.py, not the harness/fixture); ` +
          `(2) run \`${HARNESS} --no-write\`; fix a dumb crash once, else report status=crash; ` +
          `(3) if it looks like a win, run again \`${HARNESS} --tag ${it.key}\` to confirm.\n` +
          `Apply the keep/revert gate from the org code. Anchor to beat: quality > ${anchor} ` +
          `AND regressions == 0 AND p95 <= ${2 * anchorP95}.\n` +
          `Return: key, status, quality, p95, regressions, the unified \`git diff -- yaams/retrieve/\` ` +
          `as diff (EMPTY string if the gate failed and you reverted), and a one-line note.`,
        { label: `exp:${it.key}`, phase: PH, model: 'sonnet', isolation: 'worktree', schema: EXPERIMENT },
      ),
    ),
  )

  // Arbiter: keep the single best win that clears every gate.
  const wins = results
    .filter(Boolean)
    .filter(
      (r) =>
        r.status === 'ok' &&
        r.diff &&
        r.diff.trim() &&
        r.quality > anchor &&
        r.regressions === 0 &&
        (r.p95 || 0) <= 2 * anchorP95,
    )
  log(
    `round ${round}: ${results.filter(Boolean).length} ran, ${wins.length} clear the gate ` +
      `(best ${wins.length ? Math.max(...wins.map((w) => w.quality)).toFixed(4) : '—'} vs anchor ${anchor.toFixed(4)})`,
  )
  if (!wins.length) {
    dry++
    continue
  }
  dry = 0
  const best = wins.reduce((a, b) => (b.quality > a.quality ? b : a))

  // Apply the winner's diff to the campaign branch (main worktree), log + commit.
  const applied = await agent(
    `${PROGRAM}\nApply this winning experiment to the campaign branch.\n` +
      `Write the following unified diff to a temp file and \`git apply\` it to yaams/retrieve/. ` +
      `If git apply fails, report applied=false and stop (do not hand-edit).\n` +
      `Then run \`${HARNESS} --tag ${best.key}-keep\` (NO --no-write) to update the regression anchor ` +
      `state and append results.tsv. Move idea "${best.key}" to the Tried section of ` +
      `scripts/autoresearch_ideas.md as kept with its fitness delta. Commit yaams/retrieve/* + ` +
      `scripts/autoresearch_ideas.md + scripts/autoresearch_results.tsv with message ` +
      `"feat(retrieve): ${best.key} (autoresearch, q ${anchor.toFixed(4)}->${best.quality.toFixed(4)})". ` +
      `Confirm the logged quality. Return JSON {applied: bool, quality: number}.\n\n--- DIFF ---\n${best.diff}`,
    { label: `keep:${best.key}`, phase: PH, schema: { type: 'object', properties: { applied: { type: 'boolean' }, quality: { type: 'number' } }, required: ['applied'] } },
  )
  if (applied && applied.applied) {
    anchor = (applied.quality && applied.quality > anchor) ? applied.quality : best.quality
    kept.push({ key: best.key, quality: anchor, note: best.note })
    log(`KEPT ${best.key} — anchor now ${anchor.toFixed(4)}`)
  } else {
    log(`apply failed for ${best.key} — treating round as dry`)
    dry++
  }

  // Periodic Opus completeness-critic: seed fresh ideas into the ledger.
  if (round % CRITIC_EVERY === 0) {
    phase('Critic')
    await agent(
      `${PROGRAM}\nCompleteness critic. Re-read scripts/autoresearch_results.tsv and the diagnostic ` +
        `signals: replay the correction-labeled gold queries against ~/brain/autoresearch_fixture.db and ` +
        `look for ranking failure modes NOT yet in the ledger Backlog. Append 2-4 concrete, in-scope ` +
        `(ranking-only, yaams/retrieve/* excl. parse.py) untried ideas to the Backlog section of ` +
        `scripts/autoresearch_ideas.md. Commit the ledger. Return a one-line summary.`,
      { label: 'critic', phase: 'Critic', model: 'opus' },
    )
  }
}

log(`done: ${kept.length} kept wins, final anchor quality=${anchor.toFixed(4)}`)
return { kept, anchor, dryRounds: dry }
