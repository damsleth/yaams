"""Sum Jev dollars and request latency from usage.jsonl.

  .venv/bin/python scripts/jev_usage.py              # every tag
  .venv/bin/python scripts/jev_usage.py --tag jev_a1_en
"""
import argparse
import json
from collections import defaultdict

from yaams.jev import DOLLARS_PER_TOKEN, JEV_DIR


def pct(xs, p):
  xs = sorted(xs)
  return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


def summarize(tag=None, prefix=False):
  rows = defaultdict(list)
  path = JEV_DIR / "usage.jsonl"
  if path.exists():
    for line in open(path):
      r = json.loads(line)
      if tag is None or r["tag"] == tag or (prefix and r["tag"].startswith(tag)):
        rows[r["tag"]].append(r)
  out = {}
  for t, rs in rows.items():
    tok = sum(r["input_tokens"] or 0 for r in rs)
    est = sum(r.get("est_tokens") or 0 for r in rs)
    lat = [r["latency_ms"] for r in rs]
    out[t] = {"requests": len(rs), "questions": sum(r["n_questions"] for r in rs),
              "input_tokens": tok, "est_tokens": est, "dollars": round(tok * DOLLARS_PER_TOKEN, 4),
              "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95),
              "models": sorted({r.get("model") or "?" for r in rs})}
  return out


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--tag")
  ap.add_argument("--prefix", action="store_true", help="match tags starting with --tag")
  a = ap.parse_args()
  s = summarize(a.tag, a.prefix)
  for t, r in sorted(s.items()):
    print(f"{t}\t{json.dumps(r)}")
  print(f"TOTAL\t${sum(r['dollars'] for r in s.values()):.4f}\t"
        f"{sum(r['input_tokens'] for r in s.values())} tokens")


if __name__ == "__main__":
  main()
