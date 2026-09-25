"""TypeSafe Jev client: packed `noul` scoring with a score cache and usage log.

Stdlib only. Opt-in remote compute (AGENTS.md: local-only is a cost rule);
every request logs real `usage.input_tokens` so the len/3 estimate is checked.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"  # pinned: jev-latest moves on release
DOLLARS_PER_TOKEN = 0.042 / 1_000_000
MAX_QUESTIONS = 256
MAX_TOKENS = 22_400  # 64k aggregate with 30% headroom; tokenizer is not public
MAX_CHARS = 1_500
JEV_DIR = Path(os.environ.get("YAAMS_JEV_DIR", Path.home() / "brain/feed/eval/jev"))
RETRY_STATUS = {429, 500, 502, 503, 504, 529}
BACKOFF = (1, 2, 4, 8, 16, 32)

_log_lock = threading.Lock()


def est_tokens(text: str) -> int:
  return len(text) // 3 + 1  # /3 not /4: Norwegian tokenizes worse than English


def api_key() -> str:
  key = os.environ.get("TYPESAFE_API_KEY")
  if key:
    return key
  env = Path(os.environ.get("YAAMS_ENV_FILE", ".env"))
  if env.exists():
    for line in env.read_text().splitlines():
      k, _, v = line.partition("=")
      if k.strip().removeprefix("export ").strip() == "TYPESAFE_API_KEY":
        return v.strip().strip("'\"")
  raise RuntimeError("TYPESAFE_API_KEY not set (env or ./.env)")


def pack(items: list[tuple[str, str]], fixed_tokens: int, criterion: str | None,
         max_questions: int = MAX_QUESTIONS, max_tokens: int = MAX_TOKENS) -> list[list[tuple[str, str]]]:
  """Split (id, text) into requests bounded by question count and estimated tokens.
  fixed_tokens is the state; each question costs its text plus the criterion."""
  per_q = est_tokens(criterion) if criterion else 0
  out: list[list[tuple[str, str]]] = []
  cur: list[tuple[str, str]] = []
  tok = fixed_tokens
  for qid, text in items:
    t = est_tokens(text) + per_q
    if cur and (len(cur) >= max_questions or tok + t > max_tokens):
      out.append(cur)
      cur, tok = [], fixed_tokens
    cur.append((qid, text))
    tok += t
  if cur:
    out.append(cur)
  return out


def _log_usage(row: dict) -> None:
  JEV_DIR.mkdir(parents=True, exist_ok=True)
  with _log_lock, open(JEV_DIR / "usage.jsonl", "a") as f:
    f.write(json.dumps(row) + "\n")


def _post(body: dict, tag: str) -> dict | None:
  """One request with retry on 429/529/5xx. None after retries; 4xx raises."""
  data = json.dumps(body).encode()
  req = urllib.request.Request(URL, data=data, method="POST", headers={
    "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
  for wait in (*BACKOFF, None):
    t0 = time.perf_counter()
    try:
      with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
        rid = r.headers.get("x-request-id") or r.headers.get("request-id")
    except urllib.error.HTTPError as e:
      if e.code not in RETRY_STATUS:
        raise RuntimeError(f"jev {e.code}: {e.read()[:500]!r}") from e
      if wait is None:
        return None
      time.sleep(float(e.headers.get("retry-after") or wait))
      continue
    except (urllib.error.URLError, TimeoutError):
      if wait is None:
        return None
      time.sleep(wait)
      continue
    _log_usage({
      "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tag": tag,
      "model": resp.get("model"), "n_questions": len(body["questions"]),
      "input_tokens": (resp.get("usage") or {}).get("input_tokens"),
      "est_tokens": est_tokens(data.decode()),
      "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
      "request_id": rid, "cached": False})
    return resp
  return None


class _Cache:
  def __init__(self, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    self.db.execute("PRAGMA journal_mode=WAL")
    self.db.execute("CREATE TABLE IF NOT EXISTS scores (k TEXT PRIMARY KEY, v REAL)")
    self.lock = threading.Lock()

  def get(self, keys: list[str]) -> dict[str, float]:
    out = {}
    with self.lock:
      for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        q = f"SELECT k, v FROM scores WHERE k IN ({','.join('?' * len(chunk))})"
        out.update(self.db.execute(q, chunk).fetchall())
    return out

  def put(self, rows: dict[str, float]) -> None:
    with self.lock, self.db:
      self.db.executemany("INSERT OR REPLACE INTO scores VALUES (?, ?)", rows.items())


_cache: _Cache | None = None


def _get_cache() -> _Cache:
  global _cache
  if _cache is None:
    _cache = _Cache(JEV_DIR / "cache.db")
  return _cache


def cache_key(state: dict, text: str, criterion_version: str, model: str = MODEL) -> str:
  s = json.dumps(state, sort_keys=True, ensure_ascii=False)
  return hashlib.sha256(f"{model}|{criterion_version}|{s}|{text}".encode()).hexdigest()


def noul(state: dict, items: dict[str, str], criterion: str | None, *, criterion_version: str,
         tag: str, workers: int = 8, max_questions: int = MAX_QUESTIONS,
         max_tokens: int = MAX_TOKENS, use_cache: bool = True,
         stats: dict | None = None) -> dict[str, float]:
  """Score each item text against `criterion` given `state`. A failed id is
  missing from the result, never 0.0. `stats` accumulates requests/input_tokens.
  criterion=None sends no per-question `criteria`: put it in `state` once instead."""
  texts = {k: v[:MAX_CHARS] for k, v in items.items()}
  keys = {k: cache_key(state, t, criterion_version) for k, t in texts.items()}
  out: dict[str, float] = {}
  if use_cache:
    hit = _get_cache().get(list(keys.values()))
    out = {k: hit[ck] for k, ck in keys.items() if ck in hit}
  todo = [(k, t) for k, t in texts.items() if k not in out]
  state_tok = est_tokens(json.dumps(state, ensure_ascii=False))
  batches = pack(todo, state_tok, criterion, max_questions, max_tokens)

  def run(batch: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], dict | None]:
    # question ids are positional: ids never reach the model, and item ids can be long
    crit = {"criteria": {"true": criterion}} if criterion else {}
    qs = {f"q{i}": {"type": "noul", "instructions": t, **crit} for i, (_, t) in enumerate(batch)}
    return batch, _post({"model": MODEL, "state": state, "questions": qs}, tag)

  with ThreadPoolExecutor(max_workers=workers) as ex:
    for batch, resp in ex.map(run, batches):
      if stats is not None:
        stats["requests"] = stats.get("requests", 0) + 1
        stats["input_tokens"] = stats.get("input_tokens", 0) + (
          ((resp or {}).get("usage") or {}).get("input_tokens") or 0)
      if resp is None:
        continue
      answers = resp.get("answers") or {}
      got = {}
      for i, (k, _) in enumerate(batch):
        a = answers.get(f"q{i}")
        if a and isinstance(a.get("noul"), (int, float)):
          got[k] = float(a["noul"])
      out.update(got)
      if use_cache and got:
        _get_cache().put({keys[k]: v for k, v in got.items()})
  return out


def choice(state: dict, text: str, labels: dict[str, str], *, tag: str) -> tuple[str | None, dict]:
  """One `choice` question. Returns (label, probabilities); (None, {}) on failure."""
  # ponytail: uncached, one question per call; batch it when B3 needs volume
  resp = _post({"model": MODEL, "state": state, "questions": {
    "q": {"type": "choice", "instructions": text, "criteria": labels}}}, tag)
  a = ((resp or {}).get("answers") or {}).get("q") or {}
  return a.get("choice"), a.get("probabilities") or {}
