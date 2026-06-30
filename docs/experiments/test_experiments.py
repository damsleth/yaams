#!/usr/bin/env python3
"""Self-check for the experiment dataset. Runs under pytest or directly.

Guards the bits of seed.py that are easy to silently break: the final-
disposition reconciliation (a reverted overfit must read as kill, not its
round's WIN) and the kill/keep accounting. Run: python docs/experiments/test_experiments.py
"""
import json
from pathlib import Path

ROWS = [json.loads(l) for l in (Path(__file__).with_name("experiments.jsonl")).read_text().splitlines() if l.strip()]
BY = {r["key"]: r for r in ROWS}


def test_overfit_reverted_is_kill():
  # campaign.tsv records this as WIN; git reverted it as held-out overfit.
  r = BY["tier2_boost_fused_order_cap"]
  assert r["disposition"] == "kill", "reverted overfit must be kill"
  assert "REVERT" in r["note"].upper()


def test_generalizing_win_is_keep():
  assert BY["thread_coherence_credit"]["disposition"] == "keep"


def test_rerank_sweep_all_killed():
  rr = [r for r in ROWS if r["key"].startswith("rerank-k")]
  assert len(rr) == 5 and all(r["disposition"] == "kill" for r in rr)


def test_every_row_has_at_least_one_metric():
  for r in ROWS:
    assert any(v is not None for v in r["metrics"].values()), r["key"]


def test_seq_is_dense_and_ordered():
  seqs = [r["seq"] for r in ROWS]
  assert seqs == sorted(seqs) and seqs == list(range(1, len(ROWS) + 1))


def test_logger_disposition_mapping():
  import log_experiment as le
  assert le.disposition_for("rrf30-keep", "ok") == "keep"
  assert le.disposition_for("baseline-jun19", "ok") == "baseline"
  assert le.disposition_for("k80-cons105-anchor", "ok") == "baseline"
  assert le.disposition_for("some_trial", "fail:regression") == "kill"


def test_logger_append_roundtrip():
  # Append into a throwaway file (don't touch the real dataset), no rebuild.
  import json as _json

  import log_experiment as le
  orig = le.DATA
  tmp = Path(__file__).with_name(".test_tmp.jsonl")
  try:
    le.DATA = tmp
    if tmp.exists():
      tmp.unlink()
    r1 = le.append("a", "kill", {"mrr": 0.4}, era="E", rebuild=False)
    r2 = le.append("b", "keep", {"hit_rate": 0.7}, era="E", rebuild=False)
    assert r1["seq"] == 1 and r2["seq"] == 2
    lines = [_json.loads(x) for x in tmp.read_text().splitlines()]
    assert [x["key"] for x in lines] == ["a", "b"]
    assert lines[0]["metrics"]["mrr"] == 0.4
  finally:
    le.DATA = orig
    if tmp.exists():
      tmp.unlink()


if __name__ == "__main__":
  import sys
  sys.path.insert(0, str(Path(__file__).parent))
  for name, fn in sorted(globals().items()):
    if name.startswith("test_"):
      fn()
      print(f"ok  {name}")
  print(f"\n{len(ROWS)} experiments checked.")
