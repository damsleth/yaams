#!/usr/bin/env python3
"""Record one experiment to experiments.jsonl and rebuild the chart.

The single entry point for adding a point to the timeline, so the viewer at
docs/experiments/index.html never goes stale. Importable from the experiment
harnesses and runnable as a CLI for ad-hoc/manual entries.

  CLI:     python docs/experiments/log_experiment.py --key my_idea \
               --disposition kill --quality 0.62 --hit-rate 0.667 --mrr 0.49 \
               --note "why it failed"
  import:  from log_experiment import append, from_harness

`era` defaults to the contents of docs/experiments/CURRENT_ERA — bump that file
whenever the gold set or a metric definition changes, so the viewer bands the
regime shift instead of drawing a misleading slope across it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "experiments.jsonl"
ERA_FILE = HERE / "CURRENT_ERA"
BUILD = HERE / "build.py"
METRICS = ("quality", "hit_rate", "mrr", "recall@10", "latency_p95_ms")


def current_era() -> str:
  try:
    return ERA_FILE.read_text().strip() or "unknown"
  except FileNotFoundError:
    return "unknown"


def _next_seq() -> int:
  if not DATA.exists():
    return 1
  return sum(1 for ln in DATA.read_text().splitlines() if ln.strip()) + 1


def rebuild_chart() -> None:
  subprocess.run([sys.executable, str(BUILD)], check=False)


def disposition_for(tag: str, status: str) -> str:
  """Map an autoresearch tag/status to a chart disposition.

  Convention mirrors the seeded history: baseline/anchor runs are reference
  points; '-keep'/'-win' tags are adopted wins; everything else (plain trials
  and fail:* runs) is a kill until a human promotes it by re-tagging.
  """
  t = tag.lower()
  if t.startswith("baseline") or "anchor" in t:
    return "baseline"
  if t.endswith("-keep") or t.endswith("-win") or t.endswith("win"):
    return "keep"
  return "kill"


def append(key, disposition, metrics, *, era=None, date="", status="",
           delta=None, note="", commit="", rebuild=True) -> dict:
  if disposition not in ("keep", "kill", "baseline"):
    raise ValueError(f"bad disposition: {disposition!r}")
  rec = {
    "key": key,
    "date": date,
    "era": era or current_era(),
    "disposition": disposition,
    "status": status,
    "delta": delta,
    "note": note,
    "commit": commit,
    "metrics": {m: metrics.get(m) for m in METRICS},
    "seq": _next_seq(),
  }
  with DATA.open("a") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
  if rebuild:
    rebuild_chart()
  return rec


def from_harness(tag, status, summary, *, era=None, commit="", rebuild=True) -> dict:
  """Map an autoresearch_retrieval.py summary dict to an experiment row."""
  return append(
    key=tag,
    disposition=disposition_for(tag, status),
    status=status,
    era=era,
    note=f"split={summary.get('split', '')} regressions={summary.get('regressions')}",
    commit=commit,
    rebuild=rebuild,
    metrics={
      "quality": summary.get("fitness"),
      "hit_rate": summary.get("hit_rate"),
      "mrr": summary.get("mrr_partial"),
      "recall@10": summary.get("recall@10"),
      "latency_p95_ms": summary.get("retrieval_p95_ms"),
    },
  )


def _cli(argv=None) -> int:
  ap = argparse.ArgumentParser(description="Append an experiment to the timeline.")
  ap.add_argument("--key", required=True)
  ap.add_argument("--disposition", required=True, choices=["keep", "kill", "baseline"])
  ap.add_argument("--era", default=None, help="default: contents of CURRENT_ERA")
  ap.add_argument("--date", default="")
  ap.add_argument("--status", default="")
  ap.add_argument("--note", default="")
  ap.add_argument("--commit", default="")
  ap.add_argument("--delta", type=float, default=None)
  ap.add_argument("--quality", type=float)
  ap.add_argument("--hit-rate", type=float, dest="hit_rate")
  ap.add_argument("--mrr", type=float)
  ap.add_argument("--recall10", type=float, dest="recall10", help="recall@10")
  ap.add_argument("--latency-p95", type=float, dest="latency_p95_ms")
  a = ap.parse_args(argv)
  rec = append(
    a.key, a.disposition,
    {"quality": a.quality, "hit_rate": a.hit_rate, "mrr": a.mrr,
     "recall@10": a.recall10, "latency_p95_ms": a.latency_p95_ms},
    era=a.era, date=a.date, status=a.status, delta=a.delta,
    note=a.note, commit=a.commit,
  )
  print(f"logged seq {rec['seq']}: {rec['key']} [{rec['disposition']}] era={rec['era']}")
  return 0


if __name__ == "__main__":
  raise SystemExit(_cli())
