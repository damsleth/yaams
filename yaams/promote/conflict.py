"""LLM-based conflict classification for promotion candidates.

Classifies a drafted YAAMS candidate against an existing Tier 2 note to
determine whether the candidate duplicates, supplements, contradicts, or is
unrelated to the existing note.

Design contract:
- Never raises: any exception returns an unclassified verdict.
- `strip_private_fences` is applied to *both* existing and candidate
  statements before they reach the prompt so private data never leaks to
  the LLM.
- `classify_pair` is the public entry point; `_build_prompt` and
  `_parse_json_response` are helpers exposed for testing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from yaams.synthesize.llm import LLMAdapter

CONFLICT_PROMPT_VERSION = 1

Classification = Literal[
    "duplicate",
    "supplement",
    "contradict",
    "unrelated",
    "unclassified",
]

_UNCLASSIFIED_RESPONSE: dict = {
    "classification": "unclassified",
    "confidence": 0.0,
    "reason": "unparseable classifier output",
}


@dataclass
class ConflictVerdict:
    classification: Classification
    confidence: float
    reason: str
    target_path: str
    model: str | None
    prompt_version: int = CONFLICT_PROMPT_VERSION


@dataclass
class ConflictConfig:
    enabled: bool = False
    confidence_threshold: float = 0.7


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.IGNORECASE | re.DOTALL)


def strip_private_fences(text: str) -> str:
    """Remove all <private>…</private> spans (case-insensitive, multiline)."""
    return _PRIVATE_RE.sub("", text)


def _build_prompt(
    existing_title: str,
    existing_statement: str,
    candidate_title: str,
    candidate_statement: str,
) -> str:
    """Build the classifier prompt.

    Instructs the model to return bare JSON with classification/confidence/reason.
    Both statements have already been stripped of private fences by the caller.
    """
    return (
        "You are a knowledge-base deduplication assistant. "
        "Compare the two statements below and classify their relationship.\n\n"
        "Respond with ONLY a JSON object on a single line — no prose, no markdown fences:\n"
        '{"classification": "<duplicate|supplement|contradict|unrelated>", '
        '"confidence": <0.0-1.0>, "reason": "<one sentence>"}\n\n'
        "Classification definitions:\n"
        "  duplicate   — the candidate makes the same claim as the existing note\n"
        "  supplement  — the candidate adds new facts that extend the existing note\n"
        "  contradict  — the candidate makes a claim that conflicts with the existing note\n"
        "  unrelated   — the candidate is about a different topic\n\n"
        f"EXISTING NOTE: {existing_title}\n"
        f"Statement: {existing_statement}\n\n"
        f"CANDIDATE: {candidate_title}\n"
        f"Statement: {candidate_statement}\n"
    )


def _parse_json_response(text: str) -> dict:
    """Parse the LLM response as JSON.

    Handles fenced JSON (```json … ```), missing keys, and unknown enum values.
    On any error returns the canonical unclassified dict.
    """
    # Strip code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except Exception:
        return dict(_UNCLASSIFIED_RESPONSE)

    if not isinstance(data, dict):
        return dict(_UNCLASSIFIED_RESPONSE)

    # Validate classification enum
    valid_classifications = {"duplicate", "supplement", "contradict", "unrelated", "unclassified"}
    classification = str(data.get("classification", "unclassified")).lower()
    if classification not in valid_classifications:
        classification = "unclassified"

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    reason = str(data.get("reason", "unparseable classifier output"))

    return {
        "classification": classification,
        "confidence": confidence,
        "reason": reason,
    }


def classify_pair(
    existing_title: str,
    existing_statement: str,
    candidate_title: str,
    candidate_statement: str,
    target_path: str,
    adapter: "LLMAdapter",
    conflict_cfg: ConflictConfig,
) -> ConflictVerdict:
    """Classify the relationship between an existing note and a candidate.

    Strips private fences from both statements before building the prompt.
    Any exception from the adapter returns an unclassified verdict.
    If confidence < threshold, demotes to unclassified.
    """
    existing_statement_clean = strip_private_fences(existing_statement)
    candidate_statement_clean = strip_private_fences(candidate_statement)

    prompt = _build_prompt(
        existing_title,
        existing_statement_clean,
        candidate_title,
        candidate_statement_clean,
    )

    try:
        response = adapter.complete(prompt, max_tokens=200, temperature=0.1)
    except Exception:
        return ConflictVerdict(
            classification="unclassified",
            confidence=0.0,
            reason="unparseable classifier output",
            target_path=target_path,
            model=None,
        )

    parsed = _parse_json_response(response.text)
    classification: Classification = parsed["classification"]  # type: ignore[assignment]
    confidence: float = parsed["confidence"]
    reason: str = parsed["reason"]

    # Demote low-confidence results to unclassified
    if confidence < conflict_cfg.confidence_threshold:
        classification = "unclassified"

    return ConflictVerdict(
        classification=classification,
        confidence=confidence,
        reason=reason,
        target_path=target_path,
        model=response.model,
    )
