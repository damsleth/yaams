"""Golden tests for the JSON shapes yaams consumes from the `ledger` CLI.

Producing repo: **cognitive-ledger** (the `ledger` CLI; its side of the seam
is documented in that repo's docs/yaams-cogled-interface.md). Consumers in
this repo, one per shape locked below:

- `yaams/promote/dedup.py` - `ledger embed search --target ledger --query ...
  --limit 1 --json`: one JSON object with `available` (bool, missing means
  true), `reason` when unavailable, and `results` whose items carry
  `cosine_similarity` (float) and `rel_path` (str).
- `yaams/promote/dedup.py` (batch) - `ledger embed search ... --json --batch`:
  JSONL `{"query": ...}` per line on stdin, one JSON payload per line on
  stdout in input order, each line the same shape as the single response.
- `yaams/synthesize/summarize.py` - `ledger paths --json`: a JSON object with
  `ledger_notes_dir` (str). `yaams/cli/promote.py` reads the same value via
  `ledger paths --field ledger_notes_dir` (bare line on stdout).

Skipped when `ledger` is not on PATH. If a test here fails after a ledger
upgrade, the seam moved: fix the named consumer here or the producer there,
and update both repos' seam docs.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
  shutil.which("ledger") is None,
  reason="`ledger` CLI not on PATH; seam golden tests skipped",
)

_TIMEOUT = 30
_PROBE_QUERY = "yaams seam probe: deployment decision in may"


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
  return subprocess.run(
    ["ledger", *args], input=stdin, capture_output=True, text=True, timeout=_TIMEOUT,
  )


def _assert_embed_search_payload(payload: object) -> None:
  """The shape yaams/promote/dedup.py:_interpret consumes."""
  assert isinstance(payload, dict), f"payload must be a JSON object, got {type(payload)}"
  available = payload.get("available", True)
  assert isinstance(available, bool), "`available` must be a bool when present"
  if not available:
    assert isinstance(payload.get("reason", ""), str), "`reason` must be a str"
    return
  results = payload.get("results") or []
  assert isinstance(results, list), "`results` must be a list"
  for r in results:
    assert isinstance(r, dict), "each result must be an object"
    assert isinstance(float(r.get("cosine_similarity", 0.0)), float), \
      "`cosine_similarity` must be numeric"
    rel_path = r.get("rel_path")
    assert rel_path is None or isinstance(rel_path, str), "`rel_path` must be a str"


def test_embed_search_single_shape():
  out = _run([
    "embed", "search", "--target", "ledger",
    "--query", _PROBE_QUERY, "--limit", "1", "--json",
  ])
  assert out.returncode == 0, f"embed search failed: {out.stderr[:300]}"
  _assert_embed_search_payload(json.loads(out.stdout))


def test_embed_search_batch_line_shape():
  help_out = _run(["embed", "search", "--help"])
  if help_out.returncode != 0 or "--batch" not in (help_out.stdout or ""):
    pytest.skip("this ledger has no `embed search --batch` yet")
  queries = [_PROBE_QUERY, "yaams seam probe: second query"]
  stdin = "".join(json.dumps({"query": q}) + "\n" for q in queries)
  out = _run(
    ["embed", "search", "--target", "ledger", "--limit", "1", "--json", "--batch"],
    stdin=stdin,
  )
  assert out.returncode == 0, f"embed search --batch failed: {out.stderr[:300]}"
  lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
  assert len(lines) == len(queries), (
    f"--batch must emit one JSONL line per stdin query in input order; "
    f"got {len(lines)} lines for {len(queries)} queries"
  )
  for line in lines:
    _assert_embed_search_payload(json.loads(line))


def test_paths_json_shape():
  out = _run(["paths", "--json"])
  assert out.returncode == 0, f"ledger paths --json failed: {out.stderr[:300]}"
  payload = json.loads(out.stdout)
  assert isinstance(payload, dict)
  assert isinstance(payload.get("ledger_notes_dir"), str), \
    "`ledger_notes_dir` must be a str (consumed by summarize._ledger_inbox_dir)"


def test_paths_field_ledger_notes_dir():
  out = _run(["paths", "--field", "ledger_notes_dir"])
  assert out.returncode == 0, f"ledger paths --field failed: {out.stderr[:300]}"
  assert out.stdout.strip(), "`--field ledger_notes_dir` must print the bare path"
