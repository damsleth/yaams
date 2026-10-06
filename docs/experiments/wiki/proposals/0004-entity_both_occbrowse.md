# proposal 0004: entity_both_occbrowse

- date: 2026-10-06
- verdict: parked
- quality: 0.5296
- delta: +0.0013
- idea: occurrence_browse lane + participant links + name promotion for last-contact queries
- note: Gold-neutral (dev +0.0013, test 0), last-contact hit@1 0.045 -> 0.59. Parked: under the gold keep bar by construction (see P7); shipping needs owner approval for the live participant backfill.

## diff

```diff
diff --git a/yaams/retrieve/hybrid.py b/yaams/retrieve/hybrid.py
index 80f1691..d083c4f 100644
--- a/yaams/retrieve/hybrid.py
+++ b/yaams/retrieve/hybrid.py
@@ -97,6 +97,12 @@ class HybridQueryConfig:
   # tangential match can't win first/last just by being the oldest/newest.
   # 0 disables. Set by route() for first/last_occurrence, not by explicit sort.
   relevance_floor: float = 0.0
+  # Occurrence lane: a timestamp-sorted query with an entity/participant allowlist
+  # also lists the allowlisted items directly by time, so "when did I last speak
+  # with X" sees X's newest messages even when they share no words with the
+  # question (the index lanes are text matches, filtered after the fact). Lane
+  # items are exempt from relevance_floor: the allowlist is their relevance.
+  occurrence_browse: bool = False
   # Query shape forwarded from ParsedQuery so _hydrate_item can gate
   # shape-specific credits (e.g. tier2_factual_coverage_recovery).
   query_shape: str = "factual"
@@ -369,6 +375,13 @@ def query(
     # a real match. Skipped when an entity/participant filter is set: there the
     # user asked for a specific thing, and a whole-window dump would be noise.
     hydrated = _browse_window(conn, cfg, cap=hydrate_cap)
+  if cfg.occurrence_browse and cfg.sort in ("asc", "desc") and (
+    item_allow is not None or part_item_allow is not None
+  ):
+    allowed = item_allow if part_item_allow is None else (
+      part_item_allow if item_allow is None else item_allow & part_item_allow)
+    seen = {r.id for r in hydrated}
+    hydrated += [r for r in _browse_allowlist(conn, cfg, allowed, cap=cfg.top_k) if r.id not in seen]
   if cfg.rerank_enabled and hydrated:
     # Opt-in cross-encoder rerank: re-score the top `rerank_k` candidates and
     # let the cross-encoder score replace the RRF score. The pool becomes the
@@ -787,7 +800,46 @@ def _apply_relevance_floor(
   if top <= 0:
     return hydrated
   threshold = top * floor
-  return [r for r in hydrated if r.score >= threshold]
+  return [r for r in hydrated if r.score >= threshold or "occurrence_browse" in r.boosts]
+
+
+def _browse_allowlist(
+  conn: sqlite3.Connection,
+  cfg: HybridQueryConfig,
+  allowed: set[str],
+  cap: int,
+) -> list[HybridResult]:
+  """The `cap` allowlisted items nearest the sort end (newest for desc, oldest for
+  asc), honoring the same source/repo/date/lang/inferred/junk filters as the index
+  lanes. Score 0.0: the caller's timestamp sort orders them."""
+  if not allowed:
+    return []
+  order = "DESC" if cfg.sort == "desc" else "ASC"
+  rows = conn.execute(
+    f"""
+    SELECT id FROM items
+    WHERE id IN (SELECT value FROM json_each(?))
+      AND (? = '' OR source IN (SELECT value FROM json_each(?)))
+      AND (? = '' OR json_extract(raw_metadata, '$.repo') IN (SELECT value FROM json_each(?)))
+      AND (? IS NULL OR timestamp >= ?)
+      AND (? IS NULL OR timestamp <= ?)
+      AND (? IS NULL OR lang = ?)
+      AND (? = 0 OR timestamp_inferred = 0)
+      AND (? = 0 OR junk_reason IS NULL)
+      AND consolidated_into IS NULL
+    ORDER BY timestamp {order}
+    LIMIT ?
+    """,
+    (json.dumps(sorted(allowed)),) + _filter_params(cfg)
+    + (cfg.lang_filter, cfg.lang_filter, _exclude_inferred(cfg), _exclude_junk(cfg), cap),
+  ).fetchall()
+  out: list[HybridResult] = []
+  for row in rows:
+    r = _hydrate_item(conn, row["id"], ScoreComponents(), cfg)
+    if r is not None:
+      r.boosts["occurrence_browse"] = 1.0
+      out.append(r)
+  return out
 
 
 def _exclude_junk(cfg: HybridQueryConfig) -> int:
```
