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
const WIKI_EVERY = (args && args.wikiEvery) || 1     // wiki-maintainer cadence (WikiSkill, arXiv 2608.27454)
const MIN_DELTA = (args && args.minDelta) || 0.01    // keep only if quality beats anchor by >= this (noise floor; dev metric jitter is ~±0.006)
const MAX_PLAUSIBLE_QUALITY = 0.8 // anchor sanity bound: quality scale is ~0.4-0.55, so >0.8 means the baseline agent misreported (wrong field / cwd)
const HARNESS =
  '.venv/bin/python scripts/autoresearch_retrieval.py --split dev --json'
// Cross-tier wiki read: the sibling cognitive-ledger repo may keep its own
// consolidated patterns file. Candidates are checked in order relative to the
// campaign cwd; the first that exists is read alongside our own wiki, and if
// none exists the cross-read is silently off. Override with args.ledgerWiki.
const LEDGER_WIKI_CANDIDATES = (args && args.ledgerWiki)
  ? [args.ledgerWiki]
  : [
      '../cognitive-ledger/docs/wiki/patterns.md',
      '../cognitive-ledger/docs/experiments/wiki/patterns.md',
      '../cognitive-ledger/out/wiki/patterns.md',
    ]
const CROSS_WIKI = `Cross-tier wiki: if any of [${LEDGER_WIKI_CANDIDATES.join(', ')}] exists (first match wins), read it alongside our patterns.md under the same rule - never pursue an idea EITHER file marks dead; if none exists, skip the cross-read silently.`
const PROGRAM = 'IMPORTANT: this campaign lives in /Users/damsleth/code/yaams, which is NOT your current working directory. Before anything else, run `cd /Users/damsleth/code/yaams` as a standalone Bash call (the cwd persists across your subsequent Bash calls); every relative path, git command, and harness invocation below assumes that cwd. Read .plans/program-retrieve.md (the org code), docs/experiments/wiki/patterns.md (the wiki - consolidated cross-campaign knowledge; do not propose or pursue anything a pattern there marks dead), and scripts/autoresearch_ideas.md (the ledger) first. ' + CROSS_WIKI

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
    recall_dropped: { type: 'boolean' },       // harness recall@10 floor breached

    diff: { type: 'string' },                  // `git diff <HEAD> -- yaams/retrieve/` (empty if reverted)
    note: { type: 'string' },
  },
  required: ['key', 'status', 'quality', 'regressions', 'diff'],
}

phase('Baseline')
// ponytail: the harness measures `regressions` vs the PREVIOUS state-writing run, not a
// fixed golden baseline. A discarded experiment (esp. across resume/rate-limit retries) can
// leave that state pointing at code that was never committed, so the committed baseline then
// shows a phantom regression -> status fail:regression -> anchor poisoned -> 0 keeps.
// Fix: re-seed the regression reference from committed HEAD here (run WITHOUT --no-write so
// the harness writes _STATE), on a verified-clean tree, before any experiment is gated.
const base = await agent(
  `${PROGRAM}\nEstablish the anchor AND re-seed the regression reference from the committed ` +
    `baseline (no edits). The harness scores regressions against the previous state-writing run, ` +
    `so a discarded experiment can poison it — re-seeding here makes the reference match HEAD.\n` +
    `STEP 0 — verified-clean tree: \`git checkout HEAD -- yaams/retrieve/\` (discard any stray edits).\n` +
    `Then run the harness WITHOUT --no-write (this writes _STATE = committed-baseline ranks):\n` +
    `  ${HARNESS} --tag baseline-anchor\n` +
    `Return the quality, retrieval_p95_ms (as p95), rank1, and gold_queries (as gold) from its JSON, ` +
    `and the campaign-branch HEAD sha as head (\`git rev-parse HEAD\`). ` +
    `Ignore the run's own status field — the anchor quality is valid regardless; its purpose here ` +
    `is to reset the reference.`,
  { label: 'baseline', phase: 'Baseline', model: 'sonnet', schema: BASELINE },
)
let anchor = base.quality
// ponytail: guard against a misreported anchor. A baseline agent that returns the wrong
// field (e.g. recall@10 ~0.9) or runs in the wrong cwd sets an impossible bar -> every
// experiment "fails" -> a silent 0-keep campaign. Fail loudly instead of wasting a run.
if (!(typeof anchor === 'number' && anchor > 0 && anchor <= MAX_PLAUSIBLE_QUALITY)) {
  throw new Error(
    `baseline anchor quality=${anchor} is implausible (expected 0 < q <= ${MAX_PLAUSIBLE_QUALITY}); ` +
      `the baseline agent likely returned the wrong field or ran in the wrong directory. ` +
      `Aborting rather than gating a whole campaign against a bad anchor — recheck the baseline and re-run.`,
  )
}
let anchorP95 = base.p95 || 1e9
let HEAD = base.head // experiments sync retrieve/* to this; UPDATED after each kept win
// (a stale HEAD makes later experiments revert a just-applied win on their sync,
//  and the dry-round recorder then commits that reverted tree — clobbering the win)
log(`anchor quality=${anchor} p95=${anchorP95}ms rank1=${base.rank1}/${base.gold} head=${HEAD.slice(0, 8)}`)

let dry = 0
let transientFails = 0 // consecutive planner deaths (rate limits); bounded so we don't spin
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
  // ponytail: agent() returns null when the agent dies on a terminal API error
  // (e.g. rate limit) after its own retries. A null plan must not crash the run
  // (`plan.ideas` throws) nor count as "dry" (that would end a long campaign on a
  // transient blip). Retry the round up to 3x, then stop cleanly (resumable).
  if (!plan) {
    transientFails++
    log(`round ${round}: planner died (transient, likely rate limit) ${transientFails}/3`)
    if (transientFails >= 3) { log('too many consecutive planner failures — stopping (resume later)'); break }
    round-- // retry this round number rather than consuming it
    continue
  }
  transientFails = 0
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
          `Apply the keep/revert gate from the org code. Anchor to beat: quality > ${(anchor + MIN_DELTA).toFixed(4)} ` +
          `(= anchor ${anchor.toFixed(4)} + ${MIN_DELTA} min-delta noise floor; gains below that are jitter, not wins) ` +
          `AND regressions == 0 AND recall_dropped == false (harness recall@10 floor) ` +
          `AND p95 <= ${2 * anchorP95}.\n` +
          `STEP FINAL — capture \`git diff ${HEAD} -- yaams/retrieve/\` as the diff, THEN ALWAYS run ` +
          `\`git checkout ${HEAD} -- yaams/retrieve/\` to leave the working tree clean (the win, if any, ` +
          `is applied centrally from your returned diff; this keeps serial runs isolated).\n` +
          `Return: key, status, quality, hit_rate, mrr (mrr_partial), p95, regressions, recall_dropped ` +
          `(the harness recall_dropped field), the captured diff ` +
          `(EMPTY string only if you made no change; a REGRESSING experiment still returns its diff + ` +
          `metrics so we can track what regressed), plus a one-line note.`,
        { label: `exp:${it.key}`, phase: PH, model: 'sonnet', isolation: ISOLATION, schema: EXPERIMENT },
      ),
    ),
  )

  // Arbiter: keep the single best win that clears every gate.
  // Gate on the NUMBERS, not the agent's free-text status (a non-empty diff that
  // strictly beats the anchor with 0 regressions and acceptable p95 IS a win —
  // crashes have no usable diff, regressions/latency are caught explicitly).
  const wins = results
    .filter(Boolean)
    .filter(
      (r) =>
        r.diff &&
        r.diff.trim() &&
        typeof r.quality === 'number' &&
        r.quality > anchor + MIN_DELTA && // noise floor: gains below MIN_DELTA are jitter, not wins
        r.regressions === 0 &&
        r.recall_dropped !== true &&
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
      note: r.note || '',
      // full diff rides along so the recorder can preserve EVERY proposal in
      // the wiki (WikiSkill audit trail) - rejected diffs included
      diff: r.diff || '',
    }))
  const recorded = await agent(
    `${PROGRAM}\nRecord this round's experiment statistics, then apply the winner if any.\n` +
      `STEP 0 — ensure a clean tree: \`git checkout ${HEAD} -- yaams/retrieve/\` (discard any stray edits).\n` +
      `1. Append one TSV row per experiment to scripts/autoresearch_campaign.tsv (create with a header ` +
      `row if missing: round\\tkey\\tquality\\tdelta\\thit_rate\\tmrr\\tp95\\tregressions\\tstatus\\tverdict\\tnote). ` +
      `Rows (JSON): ${JSON.stringify(statRows)}\n` +
      `1b. CRITICAL — move EVERY idea tried this round (keys: ${ideas.map((it) => it.key).join(', ')}) ` +
      `from the Backlog to the Tried section of scripts/autoresearch_ideas.md with its verdict ` +
      `(kept / discarded / crashed) and delta, so the planner never re-picks a tried idea. ` +
      `Do this for discards too, not just wins.\n` +
      `1c. WIKI AUDIT TRAIL - preserve EVERY proposal, win or lose: for each experiment row above, ` +
      `write its diff field to a temp file (omit --diff-file when the diff is empty) and run ` +
      `\`.venv/bin/python docs/experiments/wiki.py --key <key> --verdict <verdict> --quality <quality> ` +
      `--delta <delta> --round ${round} --idea "<idea>" --note "<note>" --diff-file <tempfile>\`. ` +
      `Verdict mapping: the WIN row -> kept (use apply-failed if the apply below fails), ` +
      `status crash -> crashed, everything else -> discarded. Rejected diffs matter as much as ` +
      `the win: they are how later proposals account for failed attempts.\n` +
      (best
        ? `2. Apply the winning diff below: write it to a temp file and \`git apply\` it to yaams/retrieve/. ` +
          `If git apply fails, set applied=false, change that row's verdict to "apply-failed", and skip to commit. ` +
          `Then run \`${HARNESS} --tag ${best.key}-keep\` (NO --no-write) to update the anchor state + results.tsv. ` +
          `Move idea "${best.key}" to the Tried section of scripts/autoresearch_ideas.md as kept (with delta). ` +
          `3. Commit yaams/retrieve/*, scripts/autoresearch_ideas.md, scripts/autoresearch_results.tsv, ` +
          `scripts/autoresearch_campaign.tsv, and docs/experiments/wiki/ with message "feat(retrieve): ${best.key} (autoresearch, ` +
          `q ${anchor.toFixed(4)}->${best.quality.toFixed(4)})". After committing, return ` +
          `{applied: bool, quality: number, head: "<git rev-parse HEAD>"} — head is the NEW commit sha.\n` +
          `\n--- DIFF ---\n${best.diff}`
        : `2. No winner this round. Commit scripts/autoresearch_campaign.tsv, ` +
          `scripts/autoresearch_ideas.md, AND docs/experiments/wiki/ with message ` +
          `"chore(autoresearch): round ${round} stats (dry)". Return {applied: false}.`),
    { label: best ? `keep:${best.key}` : `record:r${round}`, phase: PH, model: 'sonnet',
      schema: { type: 'object', properties: { applied: { type: 'boolean' }, quality: { type: 'number' }, head: { type: 'string' } }, required: ['applied'] } },
  )
  if (best && recorded && recorded.applied) {
    anchor = recorded.quality && recorded.quality > anchor ? recorded.quality : best.quality
    if (recorded.head) HEAD = recorded.head // advance the sync target so the next round can't revert this win
    kept.push({ key: best.key, quality: anchor, note: best.note })
    dry = 0
    log(`KEPT ${best.key} — anchor now ${anchor.toFixed(4)}, HEAD ${HEAD.slice(0, 8)}`)
  } else {
    if (best) log(`apply failed for ${best.key} — round counts as dry`)
    dry++
  }

  // WikiSkill maintainer (arXiv 2608.27454): consolidate this round's raw
  // traces into the persistent wiki. Runs win or lose - the wiki compounds
  // even when every skill proposal was rejected, which is exactly when the
  // knowledge is worth keeping.
  if (round % WIKI_EVERY === 0) {
    await agent(
      `${PROGRAM}\nYou are the wiki maintainer. This round's proposals (round ${round}; keys: ` +
        `${ideas.map((it) => it.key).join(', ')}) were just preserved under docs/experiments/wiki/proposals/. ` +
        `Read them plus this round's rows in scripts/autoresearch_campaign.tsv, then consolidate into ` +
        `docs/experiments/wiki/patterns.md: where a result adds evidence for an existing pattern, extend ` +
        `that pattern's evidence list; where 2+ results (this round, or one here plus prior proposals) ` +
        `point the same way and no pattern covers it, append a new numbered pattern section with its ` +
        `evidence keys and the implication for future proposals. NEVER delete or weaken a pattern, and ` +
        `never edit proposals/ or evolution.md - the wiki is append-mostly and persists across rejected ` +
        `skill updates. A single unremarkable result consolidates to nothing: in that case make no edits. ` +
        `If you changed patterns.md, commit it with message "docs(wiki): consolidate round ${round}". ` +
        `Return a one-line summary.`,
      { label: `wiki:r${round}`, phase: PH, model: 'sonnet' },
    )
  }

  // Periodic Opus completeness-critic: seed fresh ideas into the ledger.
  if (round % CRITIC_EVERY === 0) {
    phase('Critic')
    await agent(
      `${PROGRAM}\nCompleteness critic. Re-read scripts/autoresearch_results.tsv and the diagnostic ` +
        `signals: replay the correction-labeled gold queries against ~/brain/autoresearch_fixture.db and ` +
        `look for ranking failure modes NOT yet in the ledger Backlog. Check every candidate against ` +
        `docs/experiments/wiki/patterns.md and skip anything a pattern marks dead. ${CROSS_WIKI} Append 2-4 concrete, ` +
        `in-scope (ranking-only, yaams/retrieve/* excl. parse.py) untried ideas to the Backlog section of ` +
        `scripts/autoresearch_ideas.md. Commit the ledger. Return a one-line summary.`,
      { label: 'critic', phase: 'Critic', model: 'opus' },
    )
  }
}

log(`done: ${kept.length} kept wins, final anchor quality=${anchor.toFixed(4)}`)
return { kept, anchor, dryRounds: dry }
