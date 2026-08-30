"""Sink 2: promote chat-summary facts into the Tier-2 ledger.

The capture-chat.sh hook already writes atomic, decision-centric bullets under
`## Insights / Facts` in each session summary, so no LLM drafting or
entity-clustering is needed (the entity-clustered `generate_candidates` path is
a poor fit for chats — it clusters on NER noise and truncates sources). This
path reads the bullets straight from disk via the pure `facts_from_file`
extractor and wraps each as a PromotionCandidate for the existing review/inbox/
ledger flow. It consumes the extractor directly, not ingested `chats_facts`
rows, so it works whether or not the opt-in retrieval tier is enabled.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from yaams.config import expand_path
from yaams.ingest._markdown import walk_markdown
from yaams.ingest.base import hash_id
from yaams.ingest.chats import DEFAULT_SKIP_DIRS
from yaams.ingest.chats_facts import facts_from_file
from yaams.promote.candidates import PromotionCandidate, _candidate_id
from yaams.promote.dedup import DedupChecker
from yaams.schema import FACTS_SOURCE
from yaams.time import ensure_utc

_TITLE_MAX = 80


def _fact_title(content: str) -> str:
  """Derive a concise note title from a fact bullet: drop markdown emphasis/
  backticks, prefer the first clause (punctuation followed by space, so
  in-token colons like `+00:00` don't split), cap at _TITLE_MAX."""
  text = content.strip().replace("`", "").replace("**", "")
  m = re.match(r"(.+?[.;:])(?:\s|$)", text)
  if m and len(m.group(1)) <= _TITLE_MAX:
    text = m.group(1)
  if len(text) > _TITLE_MAX:
    text = text[:_TITLE_MAX].rsplit(" ", 1)[0] + "…"
  return text.strip().rstrip(".:;") or "Untitled fact"


def generate_fact_candidates(
  chats_path: str | Path,
  since: datetime | None = None,
  dedup: DedupChecker | None = None,
  on_progress: Callable[[str], None] | None = None,
) -> list[PromotionCandidate]:
  """Extract `## Insights / Facts` bullets from every chat summary and wrap each
  as a fact PromotionCandidate. Idempotent downstream: the candidate id is
  stable per fact, so `store_candidates` (INSERT OR IGNORE) skips any fact
  already turned into a candidate (pending, accepted, or rejected)."""
  root = expand_path(Path(chats_path))
  if not root.exists():
    return []
  cutoff = ensure_utc(since) if since else None

  gathered: list[PromotionCandidate] = []
  for md_file in walk_markdown(root, set(DEFAULT_SKIP_DIRS), ("_", ".")):
    for fact in facts_from_file(md_file, root):
      if cutoff and ensure_utc(fact.timestamp) < cutoff:
        continue
      # Same id the chats_facts adapter would mint, so source_item_ids line up
      # with the raw item when the opt-in tier is also ingested (enables
      # event-time enrichment + promoted_to marking); harmless when it isn't.
      item_id = hash_id(FACTS_SOURCE, fact.source_id)
      gathered.append(PromotionCandidate(
        id=_candidate_id("", [item_id]),
        entity="",
        draft_type="fact",
        draft_title=_fact_title(fact.content),
        draft_statement=fact.content,
        draft_body="",
        draft_tags=list(fact.tags),
        source_item_ids=[item_id],
        backend="chats_facts",
      ))
  if dedup is None:
    return gathered

  # All statements are known up front here, so one --batch subprocess covers
  # the whole run when the ledger CLI supports it (see promote/dedup.py).
  dedup.prime([c.draft_statement for c in gathered])
  candidates: list[PromotionCandidate] = []
  for cand in gathered:
    verdict = dedup.check(cand.draft_statement)
    if verdict.decision == "duplicate":
      if on_progress:
        on_progress(f"  skip dup ({verdict.similarity:.2f}): {cand.draft_title}")
      continue
    if verdict.decision == "merge":
      cand.merge_with = verdict.target_path
      cand.dedup_similarity = verdict.similarity
    candidates.append(cand)
  return candidates
