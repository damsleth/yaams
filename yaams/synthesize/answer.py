"""Build a grounded synthesis prompt and parse the structured answer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from yaams.retrieve import HybridResult
from yaams.synthesize.llm import LLMAdapter, LLMResponse


CITATION_RE = re.compile(r"\[(\d+)\]")
_ANSWER_HEADER_RE = re.compile(r"(?im)^\s*ANSWER\s*:\s*")
_CONFIDENCE_HEADER_RE = re.compile(r"(?im)^\s*CONFIDENCE\s*:\s*")
_GAPS_HEADER_RE = re.compile(r"(?im)^\s*GAPS\s*:\s*")
_VALID_CONFIDENCE = {"high", "medium", "low"}


SYNTH_PROMPT_TEMPLATE = """You are answering a question using ONLY the SOURCES below. Each source is numbered.

Rules:
- Cite the source numbers you used inline as [n]. Cite at most the relevant ones.
- Do NOT use facts that are not in the SOURCES.
- If the SOURCES do not contain enough information, say so explicitly. Do not invent.
- Keep the answer brief - 1-3 short paragraphs unless the question demands more.
- Quote selectively. Do not echo whole sources.
- Match the language of the question.
- Output exactly the three sections below, in order, using the headers verbatim.

Output format:
ANSWER:
<answer with [n] citations>

CONFIDENCE: <high | medium | low>
<one short sentence on why>

GAPS:
- <bullet of what the sources did not cover>
- (or "none" on a single line if nothing is missing)

QUESTION:
{question}

SOURCES:
{sources}

ANSWER:"""


@dataclass
class AnswerResult:
  question: str
  answer: str
  answer_body: str = ""
  cited_ranks: list[int] = field(default_factory=list)
  cited_result_ids: list[str] = field(default_factory=list)
  confidence: str = "unknown"
  confidence_reason: str = ""
  gaps: list[str] = field(default_factory=list)
  backend: str = ""
  model: str | None = None
  raw_response: LLMResponse | None = None


def build_synthesis_prompt(question: str, results: Sequence[HybridResult]) -> str:
  blocks = [_render_source(rank, r) for rank, r in enumerate(results, 1)]
  return SYNTH_PROMPT_TEMPLATE.format(
    question=question.strip(),
    sources="\n\n".join(blocks) if blocks else "(no sources retrieved)",
  )


def _render_source(rank: int, result: HybridResult) -> str:
  ts = result.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(result.timestamp, "strftime") else str(result.timestamp)
  if result.kind == "consolidation":
    header = (
      f"[{rank}] {result.source} session {ts} ({result.item_count} items, "
      f"participants: {', '.join(result.participants[:5])})"
    )
  else:
    header = f"[{rank}] {result.source} {ts} from {result.sender}"
    if result.subject:
      header += f' subject="{result.subject}"'
  body = (result.content or "").strip()
  if len(body) > 1500:
    body = body[:1500] + " ..."
  return f"{header}\n{body}"


def parse_citation_ids(answer_text: str, results: Sequence[HybridResult]) -> tuple[list[int], list[str]]:
  ranks: list[int] = []
  ids: list[str] = []
  seen: set[int] = set()
  for match in CITATION_RE.finditer(answer_text or ""):
    n = int(match.group(1))
    if n in seen:
      continue
    seen.add(n)
    if 1 <= n <= len(results):
      ranks.append(n)
      ids.append(results[n - 1].id)
  return ranks, ids


def parse_structured_answer(text: str) -> tuple[str, str, str, list[str]]:
  """Split LLM output into (answer_body, confidence, confidence_reason, gaps).

  Tolerant of missing sections, missing ANSWER marker, and surrounding
  whitespace. The model occasionally drops the ANSWER header (the prompt
  ends in 'ANSWER:'), in which case the entire remainder before
  CONFIDENCE/GAPS becomes the body.
  """
  if text is None:
    return "", "unknown", "", []
  body = text.strip()
  if not body:
    return "", "unknown", "", []

  conf_match = _CONFIDENCE_HEADER_RE.search(body)
  gaps_match = _GAPS_HEADER_RE.search(body)

  end_of_answer = len(body)
  if conf_match:
    end_of_answer = min(end_of_answer, conf_match.start())
  if gaps_match:
    end_of_answer = min(end_of_answer, gaps_match.start())

  answer_section = body[:end_of_answer]
  ans_header = _ANSWER_HEADER_RE.search(answer_section)
  if ans_header:
    answer_body = answer_section[ans_header.end():].strip()
  else:
    answer_body = answer_section.strip()

  if conf_match:
    end = len(body)
    if gaps_match and gaps_match.start() > conf_match.end():
      end = gaps_match.start()
    confidence_block = body[conf_match.end():end].strip()
    confidence, confidence_reason = _split_confidence(confidence_block)
  else:
    confidence, confidence_reason = "unknown", ""

  gaps: list[str] = []
  if gaps_match:
    gaps_block = body[gaps_match.end():].strip()
    gaps = _parse_gaps(gaps_block)

  return answer_body, confidence, confidence_reason, gaps


def _split_confidence(block: str) -> tuple[str, str]:
  if not block:
    return "unknown", ""
  first_line, _, rest = block.partition("\n")
  level = first_line.strip().lower().rstrip(".")
  reason = rest.strip()
  if level not in _VALID_CONFIDENCE:
    parts = first_line.strip().split(None, 1)
    if parts and parts[0].lower().rstrip(".") in _VALID_CONFIDENCE:
      level = parts[0].lower().rstrip(".")
      reason_inline = parts[1] if len(parts) > 1 else ""
      reason = (reason_inline + ("\n" + reason if reason else "")).strip()
    else:
      level = "unknown"
      reason = (first_line + ("\n" + reason if reason else "")).strip()
  return level, reason


def _parse_gaps(block: str) -> list[str]:
  if not block:
    return []
  lowered = block.strip().lower()
  if lowered in ("none", "(none)", "- none", "no gaps"):
    return []
  lines = [line.strip() for line in block.splitlines() if line.strip()]
  cleaned: list[str] = []
  for line in lines:
    stripped = line.lstrip("-*").strip()
    if not stripped:
      continue
    if stripped.lower() == "none":
      continue
    cleaned.append(stripped)
  return cleaned


def synthesize_answer(
  question: str,
  results: Sequence[HybridResult],
  adapter: LLMAdapter,
  *,
  max_tokens: int = 600,
  temperature: float = 0.0,
) -> AnswerResult:
  prompt = build_synthesis_prompt(question, results)
  response = adapter.complete(prompt, max_tokens=max_tokens, temperature=temperature)
  body, confidence, confidence_reason, gaps = parse_structured_answer(response.text)
  ranks, ids = parse_citation_ids(response.text, results)
  return AnswerResult(
    question=question,
    answer=response.text,
    answer_body=body,
    cited_ranks=ranks,
    cited_result_ids=ids,
    confidence=confidence,
    confidence_reason=confidence_reason,
    gaps=gaps,
    backend=response.backend,
    model=response.model,
    raw_response=response,
  )
