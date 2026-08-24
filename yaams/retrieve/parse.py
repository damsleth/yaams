"""Phase H query parser.

Turns a free-form query into a `ParsedQuery` that the route layer can map
into `HybridQueryConfig` adjustments. Uses any configured LLMAdapter; the
prompt demands JSON only and the parser is robust to fenced output, missing
fields, bad dates, and hallucinated entities.

Designed to fail soft: any unexpected condition produces a fallback
`ParsedQuery` with `fallback_used=True` so the CLI can still serve a result.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yaams.synthesize.llm import LLMAdapter
from yaams.time import ensure_utc, parse_iso_datetime, utc_now

SHAPES = (
  "factual",
  "first_occurrence",
  "last_occurrence",
  "temporal_range",
  "synthesis",
  "event_anchored",
)

DEFAULT_TOP_ENTITIES = 40

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class ParsedQuery:
  raw: str
  shape: str = "factual"
  entities: list[str] = field(default_factory=list)
  date_range: tuple[datetime | None, datetime | None] = (None, None)
  topic_terms: list[str] = field(default_factory=list)
  sort: str = "relevance"
  prefer_tier: str | None = None
  high_quality: bool = False
  fallback_used: bool = False

  def to_json(self) -> str:
    start, end = self.date_range
    payload = {
      "raw": self.raw,
      "shape": self.shape,
      "entities": list(self.entities),
      "date_range": [
        start.isoformat() if start else None,
        end.isoformat() if end else None,
      ],
      "topic_terms": list(self.topic_terms),
      "sort": self.sort,
      "prefer_tier": self.prefer_tier,
      "high_quality": self.high_quality,
      "fallback_used": self.fallback_used,
    }
    return json.dumps(payload, ensure_ascii=False)


PARSE_PROMPT_TEMPLATE = """You convert a user's natural-language memory query into a strict JSON object. Output JSON ONLY. No prose, no code fences.

Today is {today}.

Schema:
{{
  "shape": one of "factual" | "first_occurrence" | "last_occurrence" | "temporal_range" | "synthesis" | "event_anchored",
  "entities": list of canonical entity names from the dictionary below; pick only matches you are confident about,
  "date_range": [start_iso_or_null, end_iso_or_null],
  "topic_terms": short list of content keywords (no entity names, no stopwords),
  "sort": "relevance" | "asc" | "desc",
  "prefer_tier": "tier2_ledger" | "raw" | null,
  "high_quality": false
}}

Shape guide:
- factual: simple lookup ("what is X", "who said Y").
- first_occurrence: when did something first happen ("when did I first hear about X"). Use sort "asc".
- last_occurrence: the most recent time something happened ("when did I last speak with X", "what's the latest on Y", "most recent message from Z"). Use sort "desc".
- temporal_range: scoped to a time window ("in April", "last week", "Q1 2026").
- synthesis: requires aggregating many sources ("what's my position on X", "summarize Y").
- event_anchored: tied to a specific meeting / event entity.

Known entities (resolve aliases to canonical names; do NOT invent):
{entity_list}

Examples:
Q: "When did I first hear about Project Atlas?"
A: {{"shape":"first_occurrence","entities":["Project Atlas"],"date_range":[null,null],"topic_terms":[],"sort":"asc","prefer_tier":null,"high_quality":false}}

Q: "When did I last speak with Alice?"
A: {{"shape":"last_occurrence","entities":["Alice"],"date_range":[null,null],"topic_terms":[],"sort":"desc","prefer_tier":null,"high_quality":false}}

Q: "What did Alice say about the spec review in April?"
A: {{"shape":"temporal_range","entities":["Alice"],"date_range":["{year}-04-01T00:00:00+00:00","{year}-04-30T23:59:59+00:00"],"topic_terms":["spec review"],"sort":"relevance","prefer_tier":null,"high_quality":false}}

Q: "What's my position on YAAMS Tier 2 promotion?"
A: {{"shape":"synthesis","entities":[],"date_range":[null,null],"topic_terms":["YAAMS","Tier 2","promotion"],"sort":"relevance","prefer_tier":"tier2_ledger","high_quality":false}}

Now parse:
Q: {query}
A:"""


def parse_query(
  text: str,
  adapter: LLMAdapter,
  conn: sqlite3.Connection | None = None,
  *,
  now: datetime | None = None,
  top_entities: int = DEFAULT_TOP_ENTITIES,
  max_tokens: int = 400,
  temperature: float = 0.0,
) -> ParsedQuery:
  raw = (text or "").strip()
  if not raw:
    return _fallback(raw)

  prompt_resolver = _build_entity_resolver(conn, top_entities)
  validation_resolver = _build_full_entity_resolver(conn, prompt_resolver)
  prompt = PARSE_PROMPT_TEMPLATE.format(
    today=(now or utc_now()).strftime("%Y-%m-%d"),
    year=(now or utc_now()).strftime("%Y"),
    entity_list=_render_entity_list(prompt_resolver),
    query=raw,
  )

  try:
    response = adapter.complete(
      prompt, max_tokens=max_tokens, temperature=temperature
    )
  except Exception:
    return _fallback(raw)

  payload = _extract_json(response.text or "")
  if payload is None:
    return _fallback(raw)

  return _coerce(raw, payload, validation_resolver)


def _fallback(raw: str) -> ParsedQuery:
  return ParsedQuery(
    raw=raw,
    shape="factual",
    entities=[],
    date_range=(None, None),
    topic_terms=[raw] if raw else [],
    sort="relevance",
    prefer_tier=None,
    high_quality=False,
    fallback_used=True,
  )


def _coerce(
  raw: str,
  payload: dict[str, Any],
  entity_resolver: "EntityResolver",
) -> ParsedQuery:
  fallback_used = False

  shape = str(payload.get("shape") or "").strip().lower()
  if shape not in SHAPES:
    shape = "factual"
    fallback_used = True

  raw_entities = payload.get("entities") or []
  if not isinstance(raw_entities, list):
    raw_entities = []
    fallback_used = True

  resolved: list[str] = []
  unresolved_topics: list[str] = []
  for ent in raw_entities:
    if not isinstance(ent, str) or not ent.strip():
      continue
    canonical = entity_resolver.resolve(ent.strip())
    if canonical:
      if canonical not in resolved:
        resolved.append(canonical)
    else:
      unresolved_topics.append(ent.strip())

  raw_terms = payload.get("topic_terms") or []
  topic_terms: list[str] = []
  if isinstance(raw_terms, list):
    for term in raw_terms:
      if isinstance(term, str) and term.strip():
        topic_terms.append(term.strip())
  for orphan in unresolved_topics:
    if orphan not in topic_terms:
      topic_terms.append(orphan)

  start, end, dr_fallback = _coerce_date_range(payload.get("date_range"))
  if dr_fallback:
    fallback_used = True

  sort = str(payload.get("sort") or "relevance").strip().lower()
  if sort not in ("relevance", "asc", "desc"):
    sort = "relevance"

  prefer = payload.get("prefer_tier")
  if isinstance(prefer, str):
    prefer_clean = prefer.strip().lower()
    prefer_tier = prefer_clean if prefer_clean in ("tier2_ledger", "raw") else None
  else:
    prefer_tier = None

  high_quality = bool(payload.get("high_quality"))

  return ParsedQuery(
    raw=raw,
    shape=shape,
    entities=resolved,
    date_range=(start, end),
    topic_terms=topic_terms,
    sort=sort,
    prefer_tier=prefer_tier,
    high_quality=high_quality,
    fallback_used=fallback_used,
  )


def _coerce_date_range(value: Any) -> tuple[datetime | None, datetime | None, bool]:
  if value is None:
    return None, None, False
  if not isinstance(value, (list, tuple)) or len(value) != 2:
    return None, None, True
  start = _coerce_date(value[0])
  end = _coerce_date(value[1])
  fallback = (value[0] is not None and start is None) or (
    value[1] is not None and end is None
  )
  return start, end, fallback


def _coerce_date(value: Any) -> datetime | None:
  if value is None or value == "":
    return None
  if isinstance(value, datetime):
    return ensure_utc(value)
  if not isinstance(value, str):
    return None
  try:
    return parse_iso_datetime(value)
  except (ValueError, TypeError):
    return None


def _extract_json(text: str) -> dict[str, Any] | None:
  body = text.strip()
  if not body:
    return None
  body = _FENCE_RE.sub("", body).strip()
  try:
    parsed = json.loads(body)
  except json.JSONDecodeError:
    match = _JSON_OBJECT_RE.search(body)
    if match is None:
      return None
    try:
      parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
      return None
  if not isinstance(parsed, dict):
    return None
  return parsed


class EntityResolver:
  def __init__(self, alias_to_canonical: dict[str, str], canonical: list[str]):
    self._aliases = alias_to_canonical
    self._canonical = canonical

  @property
  def canonical_names(self) -> list[str]:
    return list(self._canonical)

  def resolve(self, name: str) -> str | None:
    if not name:
      return None
    key = name.strip().lower()
    return self._aliases.get(key)


def _rows_to_resolver(rows) -> EntityResolver:
  """Fold (canonical_name, aliases-json) rows into an EntityResolver."""
  aliases: dict[str, str] = {}
  canonical: list[str] = []
  for row in rows:
    canon = row["canonical_name"] if hasattr(row, "keys") else row[0]
    raw_aliases = row["aliases"] if hasattr(row, "keys") else row[1]
    canonical.append(canon)
    aliases[canon.lower()] = canon
    if raw_aliases:
      try:
        alias_list = json.loads(raw_aliases)
      except (TypeError, ValueError):
        alias_list = []
      for alias in alias_list:
        if isinstance(alias, str) and alias.strip():
          aliases[alias.strip().lower()] = canon
  return EntityResolver(aliases, canonical)


def _build_entity_resolver(
  conn: sqlite3.Connection | None,
  top_n: int,
) -> EntityResolver:
  empty = EntityResolver({}, [])
  if conn is None:
    return empty

  try:
    rows = conn.execute(
      """
      SELECT e.canonical_name, e.aliases, COUNT(ie.item_id) AS hits
      FROM entities e
      LEFT JOIN item_entities ie ON ie.entity_id = e.id
      GROUP BY e.id
      ORDER BY hits DESC, e.canonical_name ASC
      LIMIT ?
      """,
      (top_n,),
    ).fetchall()
  except sqlite3.DatabaseError:
    return empty

  return _rows_to_resolver(rows)


def _build_full_entity_resolver(
  conn: sqlite3.Connection | None,
  fallback: "EntityResolver",
) -> EntityResolver:
  """Resolver covering every canonical name and alias in the DB.

  Used to validate parsed entities so long-tail names outside the prompt
  top-N still resolve. Returns the prompt resolver if the DB read fails.
  """
  if conn is None:
    return fallback

  try:
    rows = conn.execute(
      "SELECT canonical_name, aliases FROM entities"
    ).fetchall()
  except sqlite3.DatabaseError:
    return fallback

  return _rows_to_resolver(rows)


def _render_entity_list(resolver: EntityResolver) -> str:
  names = resolver.canonical_names
  if not names:
    return "(none seeded)"
  return ", ".join(names)
