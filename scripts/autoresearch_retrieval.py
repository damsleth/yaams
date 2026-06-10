#!/usr/bin/env python3
"""Autoresearch retrieval harness — the off-limits "prepare.py" analog.

Replays the judged-query set through ``yaams.retrieve.query`` and prints a
single fitness scalar (plus diagnostics) so an optimization loop has one
number to chase. The optimizing agent edits ``yaams/retrieve/*``; it must NOT
edit this file or the labeled set.

Design (see .plans/autoresearch_retrieval.md, Phase 1):

* **Gold set = hit ∪ correction feedback.** Only these labels name a known-
  correct document (``query_feedback.result_id``), so only these can be scored
  by replay. ``hit`` => the gold doc was rank 1 at judge time; ``correction``
  => the gold doc was present but mis-ranked.
* **Misses are excluded from fitness.** A ``miss`` names no correct document,
  so replay cannot verify whether a newly-surfaced result is right. We report
  the miss count and how many still return 0 / are unflippable, but they do not
  enter hit_rate or MRR. (This is a deliberate deviation from the plan's
  original ``hit_rate = #hit / (#hit+#miss+#correction)`` formula, which is
  static over labels and therefore not optimizable.)
* **Canonical replay, no LLM.** Config is reconstructed from each query's
  *stored* ``parsed_query`` JSON + ``route()`` — deterministic and fast, and it
  exercises ``route.py`` / ``hybrid.py`` (the inner-loop tunable surface)
  without an LLM at replay time. Re-parsing (``parse.py``) is a separate, slow
  experiment lane, out of scope for the inner loop.

Usage (the project venv, NOT ``uv run`` — the lockfile pins a spaCy with no
cp314 wheel, so ``uv run`` tries to re-resolve and fails; the installed venv
already has the deps):

    /Users/damsleth/code/YAAMS/.venv/bin/python3.14 scripts/autoresearch_retrieval.py [opts]

Options:
    --no-vector        FTS-only replay (the floor baseline).
    --tag NAME         Label this run in results.tsv (default: "adhoc").
    --json             Emit the summary as JSON on stdout.
    --db PATH          Override the DB (else $YAAMS_AUTORESEARCH_DB, else
                       ~/brain/autoresearch_fixture.db if it exists, else
                       /tmp/yaams_autoresearch.db, else the live config DB).
    --no-write         Don't append to results.tsv / update prev-run state.

Re-snapshot procedure (run after a reboot wipes /tmp, or to refresh the fixture):

    # 1. Copy the live DB to the stable location (one-time or after schema changes):
    cp "$(python3 -c 'from yaams.config import get_db_path, load_config; print(get_db_path(load_config()))')" \
        ~/brain/autoresearch_fixture.db

    # 2. Verify the copy has feedback rows:
    sqlite3 ~/brain/autoresearch_fixture.db \
        "SELECT kind, count(*) FROM query_feedback GROUP BY kind;"

    # 3. Optionally set the env var to skip auto-discovery:
    export YAAMS_AUTORESEARCH_DB=~/brain/autoresearch_fixture.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

# Run from the repo root regardless of cwd.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from yaams.cli._shared import _embed_config, _self_identities  # noqa: E402
from yaams.config import get_db_path, load_config  # noqa: E402
from yaams.db import open_db  # noqa: E402
from yaams.enrich import Embedder  # noqa: E402
from yaams.retrieve import (  # noqa: E402
    HybridQueryConfig,
    ParsedQuery,
    filter_results_by_entities,
    parse_query,  # noqa: F401  (imported for parity / future re-parse lane)
    route,
)
from yaams.retrieve import query as run_query  # noqa: E402
from yaams.retrieve.synonyms import normalize_synonym_groups  # noqa: E402
from yaams.time import parse_iso_datetime  # noqa: E402

_STATE = _REPO / "scripts" / ".autoresearch_state.json"
_RESULTS_TSV = _REPO / "scripts" / "autoresearch_results.tsv"
_TEST_FRACTION = 0.10  # held-out split, by stable hash of query_id

# Fitness weights (mirror the plan).
_W_HITRATE = 0.7
_W_MRR = 0.3
_LAMBDA_LATENCY = 0.10
_HARD_FAIL_LATENCY_MULT = 2.0


def _split_bucket(query_id: str) -> str:
    """Stable dev/test assignment from a hash of the query_id (not Python's
    salted hash, which varies per process)."""
    h = int(hashlib.sha1(query_id.encode()).hexdigest(), 16)
    return "test" if (h % 100) < (_TEST_FRACTION * 100) else "dev"


def _parsed_from_json(raw_json: str | None, fallback_raw: str) -> ParsedQuery | None:
    if not raw_json:
        return None
    d = json.loads(raw_json)
    dr = d.get("date_range") or [None, None]
    start = parse_iso_datetime(dr[0]) if dr and dr[0] else None
    end = parse_iso_datetime(dr[1]) if dr and len(dr) > 1 and dr[1] else None
    return ParsedQuery(
        raw=d.get("raw") or fallback_raw,
        shape=d.get("shape") or "factual",
        entities=list(d.get("entities") or []),
        date_range=(start, end),
        topic_terms=list(d.get("topic_terms") or []),
        sort=d.get("sort") or "relevance",
        prefer_tier=d.get("prefer_tier"),
        high_quality=bool(d.get("high_quality")),
        fallback_used=bool(d.get("fallback_used")),
    )


def _load_gold(conn) -> tuple[list[dict], int, int]:
    """Latest feedback row per query_id. Returns (gold_rows, n_miss,
    n_miss_zero_result). A gold row is a hit/correction with a known-correct
    result_id, joined to its query's stored retrieval config."""
    # Latest feedback row per query_id (highest id wins; resolves the 11
    # double-judged queries to their most recent verdict).
    rows = conn.execute(
        """
        SELECT f.query_id, f.kind, f.result_id,
               q.text, q.top_k, q.source_filter, q.since, q.until, q.parsed_query
        FROM query_feedback f
        JOIN (SELECT query_id, MAX(id) AS mid FROM query_feedback GROUP BY query_id) last
          ON last.mid = f.id
        JOIN queries q ON q.id = f.query_id
        """
    ).fetchall()

    gold: list[dict] = []
    n_miss = 0
    n_miss_zero = 0
    for r in rows:
        if r["kind"] in ("hit", "correction") and r["result_id"]:
            gold.append(dict(r))
        elif r["kind"] == "miss":
            n_miss += 1
            rr = conn.execute(
                "SELECT results_returned FROM queries WHERE id = ?", (r["query_id"],)
            ).fetchone()
            if rr and (rr["results_returned"] or 0) == 0:
                n_miss_zero += 1
    return gold, n_miss, n_miss_zero


def _replay_one(
    conn,
    embedder,
    self_ids,
    row: dict,
    synonym_groups: list[list[str]],
) -> tuple[int | None, float]:
    """Return (rank_of_gold_doc_or_None, retrieval_ms) for one gold query."""
    text = row["text"]
    parsed = _parsed_from_json(row["parsed_query"], text)
    sf = json.loads(row["source_filter"] or "[]") or None
    base = HybridQueryConfig(
        top_k=row["top_k"] or 10,
        source_filter=sf,
        since=parse_iso_datetime(row["since"]) if row["since"] else None,
        until=parse_iso_datetime(row["until"]) if row["until"] else None,
        synonym_groups=synonym_groups,
    )
    if parsed is not None:
        qcfg = route(parsed, base, self_identities=self_ids)
    else:
        qcfg = base

    fts_text = (
        " ".join(parsed.topic_terms) if parsed is not None and parsed.topic_terms else text
    )
    embedding = embedder.embed_batch([text])[0] if embedder is not None else None

    t0 = time.perf_counter()
    results = run_query(conn, fts_text, embedding=embedding, config=qcfg)
    if parsed is not None and qcfg.entity_filter:
        results = filter_results_by_entities(results, conn, qcfg.entity_filter)
    ms = (time.perf_counter() - t0) * 1000.0

    gold_id = row["result_id"]
    rank = None
    for i, res in enumerate(results, 1):
        if res.id == gold_id:
            rank = i
            break
    return rank, ms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vector", action="store_true", help="FTS-only (floor baseline)")
    ap.add_argument("--tag", default="adhoc")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev",
                    help="Score only this bucket (loop should use 'dev').")
    args = ap.parse_args()

    cfg = load_config()
    retrieve_cfg = cfg.get("retrieve")
    raw_synonyms = retrieve_cfg.get("synonyms") if isinstance(retrieve_cfg, dict) else None
    synonym_groups = normalize_synonym_groups(raw_synonyms)
    _stable_fixture = Path.home() / "brain" / "autoresearch_fixture.db"
    _tmp_fixture = Path("/tmp/yaams_autoresearch.db")
    db_path = (
        args.db
        or os.environ.get("YAAMS_AUTORESEARCH_DB")
        or (str(_stable_fixture) if _stable_fixture.exists() else None)
        or (str(_tmp_fixture) if _tmp_fixture.exists() else None)
        or str(get_db_path(cfg))
    )
    self_ids = _self_identities(cfg)

    status = "ok"
    try:
        conn = open_db(db_path, readonly=True)
        gold, n_miss, n_miss_zero = _load_gold(conn)
        gold = [g for g in gold if args.split == "all" or _split_bucket(g["query_id"]) == args.split]
        if not gold:
            raise RuntimeError("no gold (hit/correction) labels found for split")

        embedder = None if args.no_vector else Embedder(**_embed_config(cfg), quiet=True)

        ranks: dict[str, int | None] = {}
        latencies: list[float] = []
        n_corr_total = 0
        corr_recip: list[float] = []
        for row in gold:
            rank, ms = _replay_one(conn, embedder, self_ids, row, synonym_groups)
            ranks[row["query_id"]] = rank
            latencies.append(ms)
            if row["kind"] == "correction":
                n_corr_total += 1
                corr_recip.append(1.0 / rank if rank else 0.0)
        conn.close()
    except Exception as exc:  # noqa: BLE001 — crash => fitness 0, per plan
        out = {"fitness": 0.0, "status": "crash", "error": str(exc), "tag": args.tag}
        print(json.dumps(out) if args.as_json else f"\n---\nstatus: crash\nerror: {exc}")
        return 1

    n = len(gold)
    n_rank1 = sum(1 for r in ranks.values() if r == 1)
    hit_rate = n_rank1 / n
    mrr = sum((1.0 / r) if r else 0.0 for r in ranks.values()) / n
    mrr_partial = (sum(corr_recip) / n_corr_total) if n_corr_total else 0.0
    recall10 = sum(1 for r in ranks.values() if r and r <= 10) / n
    quality = _W_HITRATE * hit_rate + _W_MRR * mrr_partial
    p50 = median(latencies) if latencies else 0.0
    p95 = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0.0

    # Regression check vs previous run of the same vector mode + split.
    prev = {}
    if _STATE.exists():
        try:
            prev = json.loads(_STATE.read_text())
        except Exception:  # noqa: BLE001
            prev = {}
    prev_key = f"{args.split}:{'fts' if args.no_vector else 'hybrid'}"
    prev_ranks = (prev.get(prev_key) or {}).get("ranks", {})
    regressions = [
        qid for qid, pr in prev_ranks.items()
        if pr == 1 and ranks.get(qid) not in (1,)
    ]
    prev_p95 = (prev.get(prev_key) or {}).get("p95_ms")

    latency_penalty = 0.0
    if prev_p95:
        latency_penalty = _LAMBDA_LATENCY * max(0.0, p95 / prev_p95 - 1.0)
    fitness = quality - latency_penalty
    if prev_p95 and p95 > _HARD_FAIL_LATENCY_MULT * prev_p95:
        status = "fail:latency"
        fitness = 0.0
    if regressions:
        status = "fail:regression"

    summary = {
        "tag": args.tag,
        "mode": "fts" if args.no_vector else "hybrid",
        "split": args.split,
        "fitness": round(fitness, 4),
        "quality": round(quality, 4),
        "hit_rate": round(hit_rate, 4),
        "mrr": round(mrr, 4),
        "mrr_partial": round(mrr_partial, 4),
        "recall@10": round(recall10, 4),
        "retrieval_p50_ms": round(p50, 1),
        "retrieval_p95_ms": round(p95, 1),
        "gold_queries": n,
        "rank1": n_rank1,
        "corrections_in_split": n_corr_total,
        "regressions": len(regressions),
        "miss_labels_excluded": n_miss,
        "miss_zero_result": n_miss_zero,
        "status": status,
    }

    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        print("\n---")
        for k in ("fitness", "quality", "hit_rate", "mrr", "mrr_partial", "recall@10",
                  "retrieval_p50_ms", "retrieval_p95_ms", "gold_queries", "rank1",
                  "corrections_in_split", "regressions", "miss_labels_excluded",
                  "miss_zero_result", "status"):
            print(f"{k+':':22} {summary[k]}")
        if regressions:
            print(f"regressed_query_ids:   {regressions[:10]}")

    if not args.no_write:
        # results.tsv: tag mode split fitness hit_rate mrr p95 regressions status
        header = "tag\tmode\tsplit\tfitness\thit_rate\tmrr_partial\tp95_ms\tregressions\tstatus\n"
        if not _RESULTS_TSV.exists():
            _RESULTS_TSV.write_text(header)
        with _RESULTS_TSV.open("a") as fh:
            fh.write(
                f"{args.tag}\t{summary['mode']}\t{args.split}\t{summary['fitness']}\t"
                f"{summary['hit_rate']}\t{summary['mrr_partial']}\t{summary['retrieval_p95_ms']}\t"
                f"{summary['regressions']}\t{status}\n"
            )
        prev[prev_key] = {"ranks": ranks, "p95_ms": p95, "tag": args.tag}
        _STATE.write_text(json.dumps(prev))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
