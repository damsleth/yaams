"""Best-of-N majority logic in the rejudge verify pass (no LLM, stub backend)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
  "llm_judge_unjudged", Path(__file__).resolve().parent.parent / "scripts" / "llm_judge_unjudged.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class _StubLLM:
  """Returns a scripted sequence of verdict bools as JSON completions."""
  def __init__(self, verdicts):
    self._v = list(verdicts)
    self.calls = 0
  def complete(self, prompt, max_tokens=0):
    self.calls += 1
    correct = self._v.pop(0)
    return type("R", (), {"text": f'{{"correct": {str(correct).lower()}}}'})()


def test_majority_keeps_and_early_outs():
  llm = _StubLLM([True, True, False])  # 2/3 yes -> keep, and stop after 2
  assert _mod._verify_correct(llm, "p", votes=3) is True
  assert llm.calls == 2  # early-out: didn't make the 3rd call


def test_minority_rejects():
  llm = _StubLLM([True, False, False])  # 1/3 yes -> reject
  assert _mod._verify_correct(llm, "p", votes=3) is False
  assert llm.calls == 3


def test_errored_votes_count_as_no():
  class _BoomLLM:
    def complete(self, *a, **k):
      raise RuntimeError("boom")
  assert _mod._verify_correct(_BoomLLM(), "p", votes=3) is False


if __name__ == "__main__":
  test_majority_keeps_and_early_outs()
  test_minority_rejects()
  test_errored_votes_count_as_no()
  print("ok")
