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
// Worktrees are only needed to isolate CONCURRENT edits. At fanout 1 the loop is
// serial, so it runs experiments in the main checkout (no worktree) — which also
// dodges the stale-base / main-checkout-contamination issues worktrees caused.
const ISOLATION = FANOUT > 1 ? 'worktree' : undefined
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
    head: { type: 'string' }, // campaign-branch HEAD sha — the base every experiment must sync to
  },
  required: ['quality', 'p95', 'head'],
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
    hit_rate: { type: 'number' },
    mrr: { type: 'number' },                   // mrr_partial from the harness JSON
    p95: { type: 'number' },
    regressions: { type: 'number' },
    diff: { type: 'string' },                  // `git diff <HEAD> -- yaams/retrieve/` (empty if reverted)
    note: { type: 'string' },
  },
  required: ['key', 'status', 'quality', 'regressions', 'diff'],
}

phase('Baseline')
const base = await agent(
  `${PROGRAM}\nRun the harness once to establish the anchor (no edits):\n` +
    `  ${HARNESS} --no-write\n` +
    `Return the quality, retrieval_p95_ms (as p95), rank1, and gold_queries (as gold) from its JSON, ` +
    `and the campaign-branch HEAD sha as head (\`git rev-parse HEAD\`).`,
  { label: 'baseline', phase: 'Baseline', model: 'sonnet', schema: BASELINE },
)
let anchor = base.quality
let anchorP95 = base.p95 || 1e9
const HEAD = base.head // every experiment syncs retrieve/* to this before editing
log(`anchor quality=${anchor} p95=${anchorP95}ms rank1=${base.rank1}/${base.gold} head=${HEAD.slice(0, 8)}`)

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
    { label: 'plan', phase: PH, model: 'sonnet', schema: PLAN },
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
          `STEP 0 — start from clean, current code. Run \`git checkout ${HEAD} -- yaams/retrieve/\` ` +
          `(syncs to campaign HEAD whether you're in a stale worktree or the main checkout). ` +
          `Verify (e.g. \`grep -c TEMPORAL_CONS_BOOST yaams/retrieve/route.py\` should be > 0).\n` +
          `Then: (1) edit only yaams/retrieve/* (not parse.py, not the harness/fixture); ` +
          `(2) run \`${HARNESS} --no-write\` (NEVER without --no-write — do not write results.tsv); ` +
          `fix a dumb crash once, else report status=crash; (3) if it looks like a win, run ` +
          `\`${HARNESS} --no-write\` once more to confirm the number repeats.\n` +
          `Apply the keep/revert gate from the org code. Anchor to beat: quality > ${anchor} ` +
          `AND regressions == 0 AND p95 <= ${2 * anchorP95}.\n` +
          `STEP FINAL — capture \`git diff ${HEAD} -- yaams/retrieve/\` as the diff, THEN ALWAYS run ` +
          `\`git checkout ${HEAD} -- yaams/retrieve/\` to leave the working tree clean (the win, if any, ` +
          `is applied centrally from your returned diff; this keeps serial runs isolated).\n` +
          `Return: key, status, quality, hit_rate, mrr (mrr_partial), p95, regressions, the captured diff ` +
          `(EMPTY string only if you made no change; a REGRESSING experiment still returns its diff + ` +
          `metrics so we can track what regressed), plus a one-line note.`,
        { label: `exp:${it.key}`, phase: PH, model: 'sonnet', isolation: ISOLATION, schema: EXPERIMENT },
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
  const best = wins.length ? wins.reduce((a, b) => (b.quality > a.quality ? b : a)) : null

  // ALWAYS record every experiment's metrics (improve AND regress), then apply
  // the winner if there is one. Runs in the main checkout so the stats file
  // survives (worktree results.tsv writes are discarded). Stats columns:
  // round  key  quality  delta_vs_anchor  hit_rate  mrr  p95  regressions  status  verdict  note
  const ideaByKey = Object.fromEntries(ideas.map((it) => [it.key, it.idea]))
  const statRows = results
    .filter(Boolean)
    .map((r) => ({
      round, key: r.key, idea: ideaByKey[r.key] || '',
      quality: r.quality, delta: +(r.quality - anchor).toFixed(4),
      hit_rate: r.hit_rate ?? '', mrr: r.mrr ?? '', p95: r.p95 ?? '',
      regressions: r.regressions, status: r.status,
      verdict: best && r.key === best.key ? 'WIN' : 'discard',
    }))
  const recorded = await agent(
    `${PROGRAM}\nRecord this round's experiment statistics, then apply the winner if any.\n` +
      `STEP 0 — ensure a clean tree: \`git checkout ${HEAD} -- yaams/retrieve/\` (discard any stray edits).\n` +
      `1. Append one TSV row per experiment to scripts/autoresearch_campaign.tsv (create with a header ` +
      `row if missing: round\\tkey\\tquality\\tdelta\\thit_rate\\tmrr\\tp95\\tregressions\\tstatus\\tverdict\\tnote). ` +
      `Rows (JSON): ${JSON.stringify(statRows)}\n` +
      (best
        ? `2. Apply the winning diff below: write it to a temp file and \`git apply\` it to yaams/retrieve/. ` +
          `If git apply fails, set applied=false, change that row's verdict to "apply-failed", and skip to commit. ` +
          `Then run \`${HARNESS} --tag ${best.key}-keep\` (NO --no-write) to update the anchor state + results.tsv. ` +
          `Move idea "${best.key}" to the Tried section of scripts/autoresearch_ideas.md as kept (with delta). ` +
          `3. Commit yaams/retrieve/*, scripts/autoresearch_ideas.md, scripts/autoresearch_results.tsv, and ` +
          `scripts/autoresearch_campaign.tsv with message "feat(retrieve): ${best.key} (autoresearch, ` +
          `q ${anchor.toFixed(4)}->${best.quality.toFixed(4)})". Return {applied: bool, quality: number}.\n` +
          `\n--- DIFF ---\n${best.diff}`
        : `2. No winner this round. Commit just scripts/autoresearch_campaign.tsv with message ` +
          `"chore(autoresearch): round ${round} stats (dry)". Return {applied: false}.`),
    { label: best ? `keep:${best.key}` : `record:r${round}`, phase: PH, model: 'sonnet',
      schema: { type: 'object', properties: { applied: { type: 'boolean' }, quality: { type: 'number' } }, required: ['applied'] } },
  )
  if (best && recorded && recorded.applied) {
    anchor = recorded.quality && recorded.quality > anchor ? recorded.quality : best.quality
    kept.push({ key: best.key, quality: anchor, note: best.note })
    dry = 0
    log(`KEPT ${best.key} — anchor now ${anchor.toFixed(4)}`)
  } else {
    if (best) log(`apply failed for ${best.key} — round counts as dry`)
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
