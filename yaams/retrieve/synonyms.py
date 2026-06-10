"""Query-time synonym expansion driven by the entity dictionary.

The `entities` table already carries a curated `aliases` list per canonical
name (e.g. Norconsult -> ["nc", "NC"]). That data unifies surface forms at
*index* time via the dictionary tagger, but raw FTS search is literal: a
query for "nc" will not reach documents that only say "Norconsult".

This module reuses the same alias data as a synonym map so the FTS query can
be expanded: a token matching any surface form is OR'd together with every
other form in its group. It is deliberately parser-independent - it operates
on the FTS token list, so it works even when the LLM query parser falls back.

Synonymy is substitution (the forms ARE the same thing); that is exactly what
OR-expansion expresses. Looser "these travel together" associations are a
different problem handled elsewhere (see the association layer).
"""

from __future__ import annotations

import json
import sqlite3


def normalize_synonym_groups(raw: object) -> list[list[str]]:
  """Normalize config ``retrieve.synonyms`` into term groups."""
  if raw is None:
    return []
  if not isinstance(raw, list):
    raise ValueError("Config section 'retrieve.synonyms' must be a list")

  groups: list[list[str]] = []
  for group in raw:
    if not isinstance(group, list):
      raise ValueError("Each retrieve.synonyms entry must be a list")
    terms = [term.strip() for term in group if isinstance(term, str) and term.strip()]
    if len(terms) >= 2:
      groups.append(terms)
  return groups


def load_synonym_groups(
  conn: sqlite3.Connection,
  configured_groups: list[list[str]] | None = None,
) -> dict[str, list[str]]:
  """Map each casefolded surface form to every surface form in its group.

  A group is one canonical name plus its aliases. Both the canonical name
  and each alias key into the same ordered, de-duplicated list, so a token
  matching any member expands to the whole group. Returns an empty map if
  the entities table is unreadable (fail soft).
  """
  groups: dict[str, list[str]] = {}
  for group in configured_groups or []:
    _merge_group(groups, group)
  try:
    # Skip denied entities (pending_review = 2) so pruned junk does not expand.
    rows = conn.execute(
      "SELECT canonical_name, aliases FROM entities WHERE pending_review != 2"
    ).fetchall()
  except sqlite3.DatabaseError:
    return groups

  for row in rows:
    canonical = row["canonical_name"] if hasattr(row, "keys") else row[0]
    raw_aliases = row["aliases"] if hasattr(row, "keys") else row[1]
    if not canonical:
      continue
    forms: list[str] = [str(canonical)]
    if raw_aliases:
      try:
        alias_list = json.loads(raw_aliases)
      except (TypeError, ValueError):
        alias_list = []
      for alias in alias_list:
        if isinstance(alias, str) and alias.strip():
          forms.append(alias.strip())

    _merge_group(groups, forms)
  return groups


def _merge_group(groups: dict[str, list[str]], forms: list[str]) -> None:
  seen: set[str] = set()
  uniq: list[str] = []
  for form in forms:
    clean = form.strip()
    key = clean.casefold()
    if clean and key not in seen:
      seen.add(key)
      uniq.append(clean)
  if len(uniq) < 2:
    return

  merged: list[str] = []
  for form in uniq:
    existing = groups.get(form.casefold())
    if existing:
      merged.extend(existing)
    merged.append(form)

  seen.clear()
  out: list[str] = []
  for form in merged:
    key = form.casefold()
    if key not in seen:
      seen.add(key)
      out.append(form)
  for form in out:
    groups[form.casefold()] = out


def expand_fts_tokens(
  tokens: list[str],
  groups: dict[str, list[str]],
) -> list[str]:
  """Expand each token to its full synonym group, preserving order.

  Tokens with no group pass through unchanged. Matching is single-token on
  the query side (casefolded); a group member may itself be a multi-word
  phrase, which the caller is expected to quote for FTS. De-duplicates
  across the whole expanded list so repeated forms are not OR'd twice.
  """
  if not groups:
    return list(tokens)
  out: list[str] = []
  seen: set[str] = set()
  for token in tokens:
    forms = groups.get(token.casefold(), [token])
    for form in forms:
      key = form.casefold()
      if key not in seen:
        seen.add(key)
        out.append(form)
  return out
