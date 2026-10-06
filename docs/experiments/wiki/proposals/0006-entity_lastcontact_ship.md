# proposal 0006: entity_lastcontact_ship

- date: 2026-10-06
- verdict: kept
- quality: 0.5296
- delta: +0.0013
- idea: ship last-contact: participant links on ingest, promotion in parse_query, entity-gated occurrence lane on by default
- note: Kept by owner decision on the last-contact eval (0.07 -> 0.82); gold neutral. Lane gated on an entity filter after review of the participant-only case.

## diff

```diff
diff --git a/yaams/cli/ingest.py b/yaams/cli/ingest.py
index 4efdde5..4072240 100644
--- a/yaams/cli/ingest.py
+++ b/yaams/cli/ingest.py
@@ -20,6 +20,7 @@ from yaams.cli._shared import (
   _embedding_dim,
   _entity_dictionary,
   _format_duration,
+  _self_identities,
   _size_mb,
   config_option,
 )
@@ -309,6 +310,13 @@ def ingest(
     # `entities discover` / `import-people`) and de-dupes aliases. Skipped on
     # dry runs and when there is no JSON store (legacy inline dictionaries).
     entity_cleanup = None if dry_run else _cleanup_entity_dictionary(cfg)
+    # Participant links: NER only tags content, so link this run's items to the
+    # people who sent or received them (last-contact queries filter on these).
+    participant_links = None
+    if not dry_run:
+      from yaams.enrich.participants import link_participants
+
+      participant_links = link_participants(conn, _self_identities(cfg), since=run_started_at)
     # Junk annotation (quality.annotate_on_ingest): mechanical rules, then the
     # optional junk model. Off by default; never fails the ingest.
     junk_stats = None
@@ -330,6 +338,8 @@ def ingest(
       )
       if junk_stats is not None:
         envelope["stats"]["junk"] = junk_stats
+      if participant_links is not None:
+        envelope["stats"]["participant_links"] = participant_links
       summary = _post_ingest_summary(conn, cfg, run_started_at, run_stats, dry_run)
       if summary is not None:
         envelope["stats"]["summary"] = {
@@ -350,6 +360,8 @@ def ingest(
       total_duration_ms=total_duration_ms,
       entity_cleanup=entity_cleanup,
     )
+    if participant_links and participant_links["linked"]:
+      click.echo(f"  Participant links: {participant_links['linked']:,} new")
     if junk_stats is not None:
       model = junk_stats.get("model") or {}
       counts = {k: v for k, v in {**junk_stats, **model}.items() if k.startswith(("mech:", "llm:"))}
diff --git a/yaams/retrieve/hybrid.py b/yaams/retrieve/hybrid.py
index 3075044..1bcc56e 100644
--- a/yaams/retrieve/hybrid.py
+++ b/yaams/retrieve/hybrid.py
@@ -97,12 +97,12 @@ class HybridQueryConfig:
   # tangential match can't win first/last just by being the oldest/newest.
   # 0 disables. Set by route() for first/last_occurrence, not by explicit sort.
   relevance_floor: float = 0.0
-  # Occurrence lane: a timestamp-sorted query with an entity/participant allowlist
-  # also lists the allowlisted items directly by time, so "when did I last speak
+  # Occurrence lane: a timestamp-sorted query with an entity filter also lists
+  # the allowlisted items (and consolidations) directly by time, so "when did I last speak
   # with X" sees X's newest messages even when they share no words with the
   # question (the index lanes are text matches, filtered after the fact). Lane
   # items are exempt from relevance_floor: the allowlist is their relevance.
-  occurrence_browse: bool = False
+  occurrence_browse: bool = True
   # Query shape forwarded from ParsedQuery so _hydrate_item can gate
   # shape-specific credits (e.g. tier2_factual_coverage_recovery).
   query_shape: str = "factual"
@@ -375,17 +375,13 @@ def query(
     # a real match. Skipped when an entity/participant filter is set: there the
     # user asked for a specific thing, and a whole-window dump would be noise.
     hydrated = _browse_window(conn, cfg, cap=hydrate_cap)
-  if cfg.occurrence_browse and cfg.sort in ("asc", "desc") and (
-    item_allow is not None or part_item_allow is not None
-  ):
-    def both(a: set[str] | None, b: set[str] | None) -> set[str]:
-      return (a or set()) if b is None else (b if a is None else a & b)
+  # Entity filter required: a participant filter alone ("when did I last talk
+  # about the budget") would list the owner's newest messages on any topic.
+  if cfg.occurrence_browse and cfg.sort in ("asc", "desc") and item_allow is not None:
+    items = item_allow if part_item_allow is None else item_allow & part_item_allow
+    cons = (cons_allow or set()) if part_cons_allow is None else (cons_allow or set()) & part_cons_allow
     seen = {r.id for r in hydrated}
-    hydrated += [
-      r for r in _browse_allowlist(
-        conn, cfg, both(item_allow, part_item_allow), both(cons_allow, part_cons_allow), cap=cfg.top_k)
-      if r.id not in seen
-    ]
+    hydrated += [r for r in _browse_allowlist(conn, cfg, items, cons, cap=cfg.top_k) if r.id not in seen]
   if cfg.rerank_enabled and hydrated:
     # Opt-in cross-encoder rerank: re-score the top `rerank_k` candidates and
     # let the cross-encoder score replace the RRF score. The pool becomes the
diff --git a/yaams/retrieve/parse.py b/yaams/retrieve/parse.py
index 513562c..35b080d 100644
--- a/yaams/retrieve/parse.py
+++ b/yaams/retrieve/parse.py
@@ -120,6 +120,66 @@ def parse_query(
   top_entities: int = DEFAULT_TOP_ENTITIES,
   max_tokens: int = 400,
   temperature: float = 0.0,
+) -> ParsedQuery:
+  parsed = _parse_llm(
+    text, adapter, conn, now=now, top_entities=top_entities,
+    max_tokens=max_tokens, temperature=temperature,
+  )
+  if conn is not None:
+    promote_entities(parsed, promotion_map(conn))
+  return parsed
+
+
+def promotion_map(conn: sqlite3.Connection) -> dict[str, str]:
+  """Lowercased 2-4 word person/org/place names and aliases -> canonical name."""
+  # ponytail: exact multi-word matches only; single names ("Fredrik") are too ambiguous
+  out: dict[str, str] = {}
+  try:
+    rows = conn.execute(
+      "SELECT canonical_name, aliases FROM entities WHERE entity_type IN ('person', 'org', 'place')"
+    ).fetchall()
+  except sqlite3.DatabaseError:
+    return out
+  for name, aliases in rows:
+    try:
+      alias_list = json.loads(aliases or "[]")
+    except (TypeError, ValueError):
+      alias_list = []
+    for key in (name, *alias_list):
+      k = " ".join(str(key).lower().split())
+      if 2 <= len(k.split()) <= 4:
+        out.setdefault(k, name)
+  return out
+
+
+def promote_entities(parsed: ParsedQuery, names: dict[str, str]) -> None:
+  """Add exact multi-word names from the query text that the parse left out.
+
+  The prompt lists only the top-N entities, so the LLM turns every long-tail name
+  into a topic term; a promoted name becomes an entity (and leaves topic_terms),
+  which route turns into an entity filter."""
+  words = parsed.raw.lower().replace("?", " ").replace(",", " ").split()
+  found: list[str] = []
+  for n in (4, 3, 2):
+    for i in range(len(words) - n + 1):
+      canon = names.get(" ".join(words[i:i + n]))
+      if canon and canon not in found and canon not in parsed.entities:
+        found.append(canon)
+  if found:
+    lowered = {f.lower() for f in found}
+    parsed.entities = [*parsed.entities, *found]
+    parsed.topic_terms = [t for t in parsed.topic_terms if t.lower() not in lowered]
+
+
+def _parse_llm(
+  text: str,
+  adapter: LLMAdapter,
+  conn: sqlite3.Connection | None,
+  *,
+  now: datetime | None,
+  top_entities: int,
+  max_tokens: int,
+  temperature: float,
 ) -> ParsedQuery:
   raw = (text or "").strip()
   if not raw:
```
