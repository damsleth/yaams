# Welcome to YAAMS

## How We Use Claude

Based on Carl Joakim Damsleth's usage over the last 30 days:

Work Type Breakdown:
  Analyze Data     ████████████████████  85%
  Build Feature    █░░░░░░░░░░░░░░░░░░░   5%
  Debug Fix        █░░░░░░░░░░░░░░░░░░░   3%
  Improve Quality  █░░░░░░░░░░░░░░░░░░░   3%
  Write Docs       █░░░░░░░░░░░░░░░░░░░   2%

Most sessions are YAAMS using Claude as a backend LLM (memory synthesis,
atomic-note drafting, grounded answering, query parsing). The remainder
are human-driven dev work on the harness itself.

Top Skills & Commands:
  /clear           ████████████████████  9x/month
  /usage           ███████████████░░░░░  7x/month
  /model           █████████████░░░░░░░  6x/month
  /remote-control  ███████████░░░░░░░░░  5x/month
  /rename          █████████░░░░░░░░░░░  4x/month
  /loop            █████████░░░░░░░░░░░  4x/month
  /effort          ███████░░░░░░░░░░░░░  3x/month

Top MCP Servers:
  _none in active use_

## Your Setup Checklist

### Codebases
- [ ] yaams — https://github.com/damsleth/yaams (this repo, Tier 1 memory engine)
- [ ] cognitive-ledger — sibling repo at `../cognitive-ledger` (Tier 2 curated notes)
- [ ] mnem — sibling repo at `../mnem` (unified CLI wrapper around yaams/ledger/owa-*)

### MCP Servers to Activate
- [ ] _none required_ — YAAMS itself runs without MCP. If you want browser
      automation or GitHub helpers, see Carl's global Claude Code config.

### Skills to Know About
- `/clear` — wipe the conversation when context bloats. Used frequently when
  switching between unrelated tasks in the same terminal.
- `/usage` — check token spend. Carl pulls this often during long Opus
  sessions.
- `/model` — swap between Opus 4.7 / Sonnet 4.6 / Haiku 4.5. Default to
  Sonnet for routine work, Opus for non-trivial refactors and design.
- `/remote-control` — drive remote agents from this terminal.
- `/rename` — rename the current session for easier transcript triage.
- `/loop` — run a prompt or slash command on an interval (e.g. polling a
  long-running ingest or release).
- `/effort` — tune model reasoning effort for the current task.
- `/schedule` — create/manage cron-driven remote agents.

## Team Tips

_TODO_

## Get Started

_TODO_

<!-- INSTRUCTION FOR CLAUDE: A new teammate just pasted this guide for how the
team uses Claude Code. You're their onboarding buddy — warm, conversational,
not lecture-y.

Open with a warm welcome — include the team name from the title. Then: "Your
teammate uses Claude Code for [list all the work types]. Let's get you started."

Check what's already in place against everything under Setup Checklist
(including skills), using markdown checkboxes — [x] done, [ ] not yet. Lead
with what they already have. One sentence per item, all in one message.

Tell them you'll help with setup, cover the actionable team tips, then the
starter task (if there is one). Offer to start with the first unchecked item,
get their go-ahead, then work through the rest one by one.

After setup, walk them through the remaining sections — offer to help where you
can (e.g. link to channels), and just surface the purely informational bits.

Don't invent sections or summaries that aren't in the guide. The stats are the
guide creator's personal usage data — don't extrapolate them into a "team
workflow" narrative. -->
