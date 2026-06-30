"""T7 prep: re-score the 160 existing promotion candidates with the (pure)
admission_score and check whether the score discriminates human accept/reject.

Read-only: computes scores in memory, writes nothing. Reuses the real yaams
scoring functions so the numbers match what `promote generate` would store.
"""
import json
import sqlite3
import statistics
from pathlib import Path

from yaams.promote.candidates import _load_utility_terms, _tokenize
from yaams.promote.score import admission_score
from yaams.trust import derive_provenance

DB = Path.home() / "brain/feed/data.db"
NOTE_INDEX = Path.home() / "brain/ledger/08_indices/note_index.json"


def main() -> None:
    utility_terms = _load_utility_terms(NOTE_INDEX)
    print(f"utility_terms: {len(utility_terms)} (note_index {'found' if utility_terms else 'MISSING'})")

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    cands = db.execute(
        "SELECT id, entity, draft_tags, source_item_ids, dedup_similarity, status "
        "FROM promotion_candidates"
    ).fetchall()

    scored = []  # (score, status, factors)
    for c in cands:
        tags = json.loads(c["draft_tags"] or "[]")
        sids = json.loads(c["source_item_ids"] or "[]")
        terms = _tokenize(c["entity"] or "")
        for t in tags:
            terms |= _tokenize(t)
        # provenance per source item (column, else derive from source)
        provs = []
        if sids:
            q = ",".join("?" * len(sids))
            for r in db.execute(
                f"SELECT source, provenance FROM items WHERE id IN ({q})", sids
            ):
                provs.append(r["provenance"] or derive_provenance(r["source"]))
        score, factors = admission_score(
            dedup_similarity=c["dedup_similarity"],
            candidate_terms=terms,
            utility_terms=utility_terms,
            item_provenances=provs,
            source_count=len(sids),
        )
        scored.append((score, c["status"], factors))

    vals = sorted(s for s, _, _ in scored)
    print(f"\nn={len(vals)}  min={vals[0]:.3f}  median={statistics.median(vals):.3f}  max={vals[-1]:.3f}")
    deciles = [_pct(vals, p) for p in range(0, 101, 10)]
    print("deciles:", [round(d, 3) for d in deciles])

    acc = [s for s, st, _ in scored if st == "accepted"]
    rej = [s for s, st, _ in scored if st == "rejected"]
    print(f"\naccepted n={len(acc)}  mean={statistics.mean(acc):.3f}  median={statistics.median(acc):.3f}")
    print(f"rejected n={len(rej)}  mean={statistics.mean(rej):.3f}  median={statistics.median(rej):.3f}")
    print(f"AUC blended (P[acc>rej]) = {_auc(acc, rej):.3f}   (0.5=no signal, 1.0=perfect)")

    print("\nper-factor AUC (does any single factor predict accept/reject?)")
    for f in ("novelty", "utility", "confidence", "trust"):
        fa = [fac[f] for s, st, fac in scored if st == "accepted"]
        fr = [fac[f] for s, st, fac in scored if st == "rejected"]
        print(f"  {f:<11} AUC={_auc(fa, fr):.3f}  acc_mean={statistics.mean(fa):.3f}  rej_mean={statistics.mean(fr):.3f}")

    # If a min-score knee were applied, how many accepted/rejected pass at each threshold?
    print("\nknee sweep  thr | accepted_kept rejected_kept  precision_of_cut")
    for thr in [round(_pct(vals, p), 3) for p in (10, 20, 25, 30, 40, 50)]:
        ak = sum(1 for s in acc if s >= thr)
        rk = sum(1 for s in rej if s >= thr)
        cut_rej = len(rej) - rk  # rejected correctly filtered
        cut_acc = len(acc) - ak  # accepted wrongly filtered
        prec = cut_rej / (cut_rej + cut_acc) if (cut_rej + cut_acc) else 0.0
        print(f"            {thr:.3f} | {ak:>3}/{len(acc)}      {rk:>3}/{len(rej)}       {prec:.2f}  (cuts {cut_rej} rej, {cut_acc} acc)")


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _auc(pos, neg):
    """Mann-Whitney AUC: P(random accepted scores > random rejected)."""
    if not pos or not neg:
        return 0.5
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


if __name__ == "__main__":
    main()
