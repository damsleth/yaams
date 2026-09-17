"""A rate limit must cost at most the in-flight batches, never the whole run."""
import itertools
import threading
import time

import pytest

from scripts import junk_sonnet_pass as jp


def _rows(n):
  return [{"id": f"r{i}"} for i in range(n)]


def test_failed_batch_stops_run_and_keeps_earlier_verdicts(tmp_path, monkeypatch):
  monkeypatch.setattr(jp, "CKPT_DIR", str(tmp_path))
  monkeypatch.setattr(jp, "BATCH", 1)
  monkeypatch.setattr(jp, "render", lambda conn, rows: "x")

  lock, calls = threading.Lock(), itertools.count()
  started = []

  def fake_judge(text, n):
    with lock:
      i = next(calls)
      started.append(i)
    time.sleep(0.2)  # a real call is slow; instant stubs hide the cancellation
    return None if i >= 2 else ["KEEP"] * n

  monkeypatch.setattr(jp, "judge_text", fake_judge)

  with pytest.raises(jp.RateLimited):
    jp.run_pass(None, _rows(40), workers=6, run_id=77)

  assert len(jp.load_ckpt(77)) == 2, "completed verdicts must survive the failure"
  assert len(started) < 20, "a failure must cancel the queue, not burn all 40 calls"

  # resume: the checkpointed rows are not re-judged
  monkeypatch.setattr(jp, "judge_text", lambda text, n: ["JUNK"] * n)
  assert len(jp.run_pass(None, _rows(40), workers=6, run_id=77)) == 40
