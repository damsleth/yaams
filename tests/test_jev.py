import io
import json

import pytest

from yaams import jev


class _Resp(io.BytesIO):
  headers = {"x-request-id": "r1"}

  def __enter__(self):
    return self

  def __exit__(self, *a):
    return False


@pytest.fixture
def fake(monkeypatch, tmp_path):
  monkeypatch.setattr(jev, "JEV_DIR", tmp_path)
  monkeypatch.setattr(jev, "_cache", None)
  monkeypatch.setenv("TYPESAFE_API_KEY", "k")
  calls = []

  def urlopen(req, timeout=None):
    body = json.loads(req.data)
    calls.append(body)
    qs = sorted(body["questions"])
    answers = {q: {"type": "noul", "noul": 0.25} for q in qs[1:]}  # drop the first answer
    return _Resp(json.dumps({"model": jev.MODEL, "answers": answers,
                             "usage": {"input_tokens": 10}}).encode())

  monkeypatch.setattr(jev.urllib.request, "urlopen", urlopen)
  return calls


def test_pack_respects_both_bounds():
  items = [(str(i), "x" * (i % 400)) for i in range(2000)]
  batches = jev.pack(items, fixed_tokens=100, criterion="c" * 30, max_questions=64, max_tokens=5000)
  assert sum(len(b) for b in batches) == 2000
  for b in batches:
    assert len(b) <= 64
    assert 100 + sum(jev.est_tokens(t) + jev.est_tokens("c" * 30) for _, t in b) <= 5000


def test_missing_answer_stays_missing_and_cache_skips_request(fake):
  items = {f"id{i}": f"text {i}" for i in range(5)}
  kw = dict(criterion="is junk", criterion_version="t1", tag="t", workers=1)
  out = jev.noul({"s": 1}, items, **kw)
  assert len(fake) == 1
  assert fake[0]["model"] == "jev-1.13.0"
  assert all(q["type"] == "noul" and q["criteria"] == {"true": "is junk"}
             for q in fake[0]["questions"].values())
  assert len(out) == 4 and 0.0 not in out.values()  # the dropped id is absent, not zero
  assert sorted(set(items) - set(out)) == ["id0"]
  again = jev.noul({"s": 1}, {k: items[k] for k in out}, **kw)
  assert again == out and len(fake) == 1  # all cached: no request
  usage = [json.loads(line) for line in open(jev.JEV_DIR / "usage.jsonl")]
  assert usage[0]["input_tokens"] == 10 and usage[0]["request_id"] == "r1"


def test_cache_only_never_calls(fake, monkeypatch):
  monkeypatch.setenv("YAAMS_JEV_CACHE_ONLY", "1")
  assert jev.noul({}, {"a": "x"}, "c", criterion_version="t", tag="t") == {}
  assert fake == []


def test_criterion_none_sends_no_criteria(fake):
  jev.noul({"criterion": "c"}, {"a": "x", "b": "y"}, None, criterion_version="t2", tag="t", workers=1)
  assert all("criteria" not in q for q in fake[0]["questions"].values())


def test_retry_then_give_up(monkeypatch, tmp_path):
  monkeypatch.setattr(jev, "JEV_DIR", tmp_path)
  monkeypatch.setattr(jev, "_cache", None)
  monkeypatch.setattr(jev, "BACKOFF", (0,))
  monkeypatch.setenv("TYPESAFE_API_KEY", "k")
  n = []

  def urlopen(req, timeout=None):
    n.append(1)
    raise jev.urllib.error.HTTPError(req.full_url, 529, "overloaded", {}, None)

  monkeypatch.setattr(jev.urllib.request, "urlopen", urlopen)
  assert jev.noul({}, {"a": "x"}, "c", criterion_version="t", tag="t", use_cache=False) == {}
  assert len(n) == 2
