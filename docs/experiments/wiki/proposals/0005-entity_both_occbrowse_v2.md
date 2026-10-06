# proposal 0005: entity_both_occbrowse_v2

- date: 2026-10-06
- verdict: parked
- quality: 0.5296
- delta: +0.0013
- idea: occurrence lane browses consolidations too; last-contact eval bounded at ask time
- note: Supersedes proposal 0004's numbers: last-contact hit@1 0.114 -> 0.818 with all three pieces, gold neutral. Parked pending owner approval for the live participant backfill; next lever is consolidations.participants (senders only).

## diff

```diff
diff --git a/scripts/last_contact_eval.py b/scripts/last_contact_eval.py
index 627dde3..27570de 100644
--- a/scripts/last_contact_eval.py
+++ b/scripts/last_contact_eval.py
@@ -100,9 +100,12 @@ def run(a):
   rows = []
   for c in cases:
     row = {"query_id": c["query_id"], "text": c["text"], "parsed_query": c["parsed_query"],
-           "source_filter": None, "since": None, "until": None, "ts": c["ts"], "result_id": c["latest"]}
+           "source_filter": None, "since": None, "until": c["ts"], "ts": c["ts"], "result_id": c["latest"]}
     ar._replay_one(conn, emb, _self_identities(cfg), row, syn, exclude_junk=a.exclude_junk)
-    ok = set(c["acceptable"])
+    # a consolidated conversation is retrieved as its consolidation, so that counts too
+    ok = set(c["acceptable"]) | {r[0] for r in conn.execute(
+      "SELECT DISTINCT consolidated_into FROM items WHERE consolidated_into IS NOT NULL "
+      "AND id IN (SELECT value FROM json_each(?))", (json.dumps(c["acceptable"]),))}
     ids = [r.id for r in holder["res"]]
     rank = next((i for i, x in enumerate(ids, 1) if x in ok), None)
     rows.append((c, rank, rank != 1 and bool(ids) and _newer_link(conn, ids[0], c)))
diff --git a/yaams/retrieve/hybrid.py b/yaams/retrieve/hybrid.py
index d083c4f..3075044 100644
--- a/yaams/retrieve/hybrid.py
+++ b/yaams/retrieve/hybrid.py
@@ -378,10 +378,14 @@ def query(
   if cfg.occurrence_browse and cfg.sort in ("asc", "desc") and (
     item_allow is not None or part_item_allow is not None
   ):
-    allowed = item_allow if part_item_allow is None else (
-      part_item_allow if item_allow is None else item_allow & part_item_allow)
+    def both(a: set[str] | None, b: set[str] | None) -> set[str]:
+      return (a or set()) if b is None else (b if a is None else a & b)
     seen = {r.id for r in hydrated}
-    hydrated += [r for r in _browse_allowlist(conn, cfg, allowed, cap=cfg.top_k) if r.id not in seen]
+    hydrated += [
+      r for r in _browse_allowlist(
+        conn, cfg, both(item_allow, part_item_allow), both(cons_allow, part_cons_allow), cap=cfg.top_k)
+      if r.id not in seen
+    ]
   if cfg.rerank_enabled and hydrated:
     # Opt-in cross-encoder rerank: re-score the top `rerank_k` candidates and
     # let the cross-encoder score replace the RRF score. The pool becomes the
@@ -807,14 +811,40 @@ def _browse_allowlist(
   conn: sqlite3.Connection,
   cfg: HybridQueryConfig,
   allowed: set[str],
+  allowed_cons: set[str],
   cap: int,
 ) -> list[HybridResult]:
-  """The `cap` allowlisted items nearest the sort end (newest for desc, oldest for
-  asc), honoring the same source/repo/date/lang/inferred/junk filters as the index
-  lanes. Score 0.0: the caller's timestamp sort orders them."""
-  if not allowed:
-    return []
+  """The `cap` allowlisted items and consolidations nearest the sort end (newest for
+  desc, oldest for asc), honoring the same filters as the index lanes. A consolidated
+  item is reached through its consolidation, as everywhere else. Score 0.0: the
+  caller's timestamp sort orders them."""
   order = "DESC" if cfg.sort == "desc" else "ASC"
+  out: list[HybridResult] = []
+  if allowed and cfg.include_items:
+    out += _browse_allowlist_items(conn, cfg, allowed, order, cap)
+  if allowed_cons and cfg.include_consolidations and not cfg.repo_filter:
+    rows = conn.execute(
+      f"""
+      SELECT id FROM consolidations
+      WHERE id IN (SELECT value FROM json_each(?))
+        AND (? = '' OR source IN (SELECT value FROM json_each(?)))
+        AND (? IS NULL OR end_timestamp >= ?)
+        AND (? IS NULL OR start_timestamp <= ?)
+      ORDER BY start_timestamp {order}
+      LIMIT ?
+      """,
+      (json.dumps(sorted(allowed_cons)),) + _filter_params(cfg, repo=False) + (cap,),
+    ).fetchall()
+    out += [r for r in (_hydrate_consolidation(conn, row["id"], ScoreComponents(), cfg) for row in rows) if r]
+  out.sort(key=lambda r: r.timestamp, reverse=order == "DESC")
+  for r in out[:cap]:
+    r.boosts["occurrence_browse"] = 1.0
+  return out[:cap]
+
+
+def _browse_allowlist_items(
+  conn: sqlite3.Connection, cfg: HybridQueryConfig, allowed: set[str], order: str, cap: int,
+) -> list[HybridResult]:
   rows = conn.execute(
     f"""
     SELECT id FROM items
@@ -833,13 +863,7 @@ def _browse_allowlist(
     (json.dumps(sorted(allowed)),) + _filter_params(cfg)
     + (cfg.lang_filter, cfg.lang_filter, _exclude_inferred(cfg), _exclude_junk(cfg), cap),
   ).fetchall()
-  out: list[HybridResult] = []
-  for row in rows:
-    r = _hydrate_item(conn, row["id"], ScoreComponents(), cfg)
-    if r is not None:
-      r.boosts["occurrence_browse"] = 1.0
-      out.append(r)
-  return out
+  return [r for r in (_hydrate_item(conn, row["id"], ScoreComponents(), cfg) for row in rows) if r]
 
 
 def _exclude_junk(cfg: HybridQueryConfig) -> int:
```
