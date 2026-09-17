"""LLM pass over the post-mechanical short rows: annotate llm:junk.

Usage (project venv):
  .venv/bin/python scripts/junk_sonnet_pass.py --agreement 50   # two-run agreement on a sample
  .venv/bin/python scripts/junk_sonnet_pass.py                  # dry run, one pass
  .venv/bin/python scripts/junk_sonnet_pass.py --apply          # two passes, write on agreement

Resumable: verdicts checkpoint per batch under $YAAMS_JUNK_PASS_DIR
(default ~/brain/feed/eval/junk-pass/); a rate limit stops the run and a rerun
continues from the checkpoint. ~816 batches per pass at 30 rows/batch.

Scope: messaging rows (imessage / signal / teams*) of 10-39 trimmed chars with
junk_reason IS NULL -- the band the mechanical rules cannot decide. Each row is
shown with the previous and next message in its thread, because "ok" and "ja"
are unclassifiable alone.

Modes:
  --agreement N   judge N sampled rows twice, report agreement; write nothing
  (default)       dry run: judge everything once, print would-flag counts
  --apply         judge everything twice, write llm:junk ONLY where both runs
                  say JUNK; disagreements are left NULL (i.e. kept)

Judge-agnostic: the rubric lives in `junk_verdict_prompt.md` ($YAAMS_JUNK_PROMPT)
and the judge is any command that reads it on stdin and writes "<n>: JUNK|KEEP"
lines on stdout -- `--judge` / $YAAMS_JUDGE_CMD. Use it to spend a different
provider's quota, or to pin effort explicitly (it is otherwise inherited from
whatever shell launched the run, silently):

  --judge "claude -p --model sonnet --effort low"
  --judge2 "copilot -p --model claude-sonnet-4.5"   # run 2 on another licence

Two different judges agreeing is stronger evidence than one agreeing with
itself -- but the plan's 93% agreement was measured Sonnet-vs-Sonnet, so
re-measure with --agreement before spending a full pass on a new pairing.
Each run records its judge in <ckpt>.meta, so a mixed run is visible after.

Never deletes. Reverse with: UPDATE items SET junk_reason=NULL WHERE junk_reason='llm:junk'
"""
import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from yaams.db import open_db  # noqa: E402
from yaams.store import chunked  # noqa: E402

BATCH = 30
SCOPE_SQL = """
SELECT id, source, thread_id, timestamp, sender, content FROM items
WHERE junk_reason IS NULL
  AND (source = 'imessage' OR source = 'signal' OR source LIKE 'teams%')
  AND length(trim(content)) BETWEEN 10 AND 39
ORDER BY thread_id, timestamp
"""

PROMPT_PATH = os.environ.get(
  "YAAMS_JUNK_PROMPT", os.path.join(os.path.dirname(__file__), "junk_verdict_prompt.md"))
PROMPT = open(PROMPT_PATH).read().rstrip() + "\n\n"

# The judge is any command that reads the prompt on stdin and writes
# "<n>: JUNK|KEEP" lines on stdout -- claude, a Copilot CLI, an ollama wrapper.
# Effort is NOT inherited by accident: pass it in the command if you want it.
DEFAULT_JUDGE = "claude -p --model sonnet"



def load_scope(conn):
  return [dict(r) for r in conn.execute(SCOPE_SQL)]


def context(conn, row):
  prev = conn.execute(
    "SELECT content FROM items WHERE thread_id=? AND timestamp<? ORDER BY timestamp DESC LIMIT 1",
    (row["thread_id"], row["timestamp"]),
  ).fetchone()
  nxt = conn.execute(
    "SELECT content FROM items WHERE thread_id=? AND timestamp>? ORDER BY timestamp ASC LIMIT 1",
    (row["thread_id"], row["timestamp"]),
  ).fetchone()
  return (prev[0] if prev else "")[:120], (nxt[0] if nxt else "")[:120]


def render(conn, rows):
  out = []
  for i, r in enumerate(rows, 1):
    p, n = context(conn, r)
    out.append(f"{i}. [{r['source']}] prev: {p!r}\n   TARGET: {r['content'].strip()!r}\n   next: {n!r}")
  return "\n".join(out)


class RateLimited(Exception):
  pass


# ponytail: a subscription rate-limit window is hours, not seconds. The first
# version gave up after 3 tries / 15s, so one limit ended the run. ~2h of
# patience outlasts a window; past that, fail-stop with the checkpoint intact.
BACKOFF = (60, 120, 300, 900, 1800, 1800, 1800)


def judge_text(text, n, cmd=None):
  """Worker: CLI only. Returns a verdict list, or None on failure.

  A failed call is a *failure*, never a batch of KEEP: the first version
  defaulted to KEEP and turned an hour of rate-limited calls into 24k silent
  "keep" votes that vetoed every real JUNK verdict at the agreement gate.
  """
  for attempt, backoff in enumerate(BACKOFF):
    r = subprocess.run(shlex.split(cmd or DEFAULT_JUDGE), input=text,
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not r.stdout.strip():
      why = (r.stderr or r.stdout).strip()[-300:] or f"rc={r.returncode}, empty stdout"
      print(f"    call failed (attempt {attempt + 1}/{len(BACKOFF)}), sleeping {backoff}s: {why}",
            file=sys.stderr, flush=True)
      time.sleep(backoff)
      continue
    verdict = {}
    for line in r.stdout.splitlines():
      line = line.strip().strip("`")
      if ":" in line:
        k, v = line.split(":", 1)
        if k.strip().isdigit() and v.strip().upper() in ("JUNK", "KEEP"):
          verdict[int(k)] = v.strip().upper()
    if len(verdict) >= n * 0.9:
      return [verdict.get(i, "KEEP") for i in range(1, n + 1)]
    print(f"    unparseable reply ({len(verdict)}/{n} verdicts), sleeping {backoff}s",
          file=sys.stderr, flush=True)
    time.sleep(backoff)
  return None


CKPT_DIR = os.path.expanduser(os.environ.get("YAAMS_JUNK_PASS_DIR", "~/brain/feed/eval/junk-pass"))


def ckpt_path(run_id):
  os.makedirs(CKPT_DIR, exist_ok=True)
  return os.path.join(CKPT_DIR, f"sonnet_verdicts_run{run_id}.jsonl")


def load_ckpt(run_id):
  out = {}
  pth = ckpt_path(run_id)
  if os.path.exists(pth):
    for line in open(pth):
      out.update(json.loads(line))
  return out


def run_pass(conn, rows, workers=6, run_id=1, judge=None):
  """Judge every batch not already in this run's checkpoint. Stops at the first
  failed batch (rate limit) and reports progress; rerun to resume."""
  done = load_ckpt(run_id)
  todo = [b for b in chunked(rows, BATCH) if not all(r["id"] in done for r in b)]
  print(f"run {run_id}: {len(done)} rows checkpointed, {len(todo)} batches to go "
        f"[judge: {judge or DEFAULT_JUDGE}]", file=sys.stderr, flush=True)
  # which judge produced which run, so a mixed run is visible afterwards
  with open(ckpt_path(run_id) + ".meta", "a") as mf:
    mf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{judge or DEFAULT_JUDGE}\t{len(todo)} batches\n")
  texts = [PROMPT + render(conn, b) for b in todo]
  with open(ckpt_path(run_id), "a") as ck, ThreadPoolExecutor(max_workers=workers) as ex:
    for i, (b, v) in enumerate(zip(todo, ex.map(lambda tb: judge_text(tb[0], len(tb[1]), judge), zip(texts, todo)))):
      if v is None:
        raise RateLimited(f"run {run_id}: batch {i+1}/{len(todo)} failed after retries; "
                          f"{len(load_ckpt(run_id))} rows checkpointed so far -- rerun to resume")
      ck.write(json.dumps({r["id"]: verdict for r, verdict in zip(b, v)}) + "\n")
      ck.flush()
      if i % 40 == 0:
        print(f"  run {run_id} batch {i+1}/{len(todo)}", file=sys.stderr, flush=True)
  return load_ckpt(run_id)


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--agreement", type=int, default=0)
  ap.add_argument("--apply", action="store_true")
  ap.add_argument("--limit", type=int, default=0)
  ap.add_argument("--workers", type=int, default=6)
  ap.add_argument("--judge", default=os.environ.get("YAAMS_JUDGE_CMD"),
                  help=f"command reading the prompt on stdin (default: {DEFAULT_JUDGE!r})")
  ap.add_argument("--judge2", help="different judge for run 2; cross-model agreement is "
                                   "stronger evidence than a model agreeing with itself")
  a = ap.parse_args()

  cfg = yaml.safe_load(open(os.path.expanduser("~/.config/yaams/config.yaml")))
  db = os.path.expanduser(cfg.get("db_path") or "~/brain/feed/data.db")
  conn = open_db(db, readonly=not a.apply)
  rows = load_scope(conn)
  print(f"scope: {len(rows)} rows")
  if a.limit:
    rows = rows[: a.limit]

  if a.agreement:
    random.seed(7)
    sample = random.sample(rows, min(a.agreement, len(rows)))
    v1 = run_pass(conn, sample, a.workers, run_id=91, judge=a.judge)
    v2 = run_pass(conn, sample, a.workers, run_id=92, judge=a.judge2 or a.judge)
    pairs = [(r, v1[r["id"]], v2[r["id"]]) for r in sample]
    agree = sum(1 for _, x, y in pairs if x == y)
    both = sum(1 for _, x, y in pairs if x == y == "JUNK")
    print(f"agreement: {agree}/{len(pairs)} = {agree/len(pairs):.0%}   both-JUNK {both}")
    return

  try:
    v1 = run_pass(conn, rows, a.workers, run_id=1, judge=a.judge)
    if a.apply:
      v2 = run_pass(conn, rows, a.workers, run_id=2, judge=a.judge2 or a.judge)
  except RateLimited as e:
    print(f"STOPPED: {e}")
    return

  j1 = sum(1 for r in rows if v1.get(r["id"]) == "JUNK")
  if not a.apply:
    print(f"dry run: run 1 would flag {j1}/{len(rows)} as llm:junk")
    return
  ids = [r["id"] for r in rows if v1.get(r["id"]) == v2.get(r["id"]) == "JUNK"]
  disagree = sum(1 for r in rows if v1.get(r["id"]) != v2.get(r["id"]))
  for chunk in chunked(ids):
    ph = ",".join("?" * len(chunk))
    conn.execute(f"UPDATE items SET junk_reason='llm:junk' WHERE junk_reason IS NULL AND id IN ({ph})", chunk)
  conn.commit()
  print(f"applied llm:junk to {len(ids)}/{len(rows)}   run1 JUNK {j1}   disagreements kept {disagree}")


if __name__ == "__main__":
  main()
