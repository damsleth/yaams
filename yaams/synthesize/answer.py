"""Build a grounded synthesis prompt and parse the answer for citations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from yaams.retrieve import HybridResult
from yaams.synthesize.llm import LLMAdapter, LLMResponse


CITATION_RE = re.compile(r"\[(\d+)\]")


SYNTH_PROMPT_TEMPLATE = """You are answering a question using ONLY the SOURCES below. Each source is numbered.

Rules:
- Cite the source numbers you used inline as [n]. Cite at most the relevant ones.
- Do NOT use facts that are not in the SOURCES.
- If the SOURCES do not contain enough information, say so explicitly. Do not invent.
- Keep the answer brief - 1-3 short paragraphs unless the question demands more.
- Quote selectively. Do not echo whole sources.
- Match the language of the question.

QUESTION:
{question}

SOURCES:
{sources}

ANSWER:"""


@dataclass
class AnswerResult:
  question: str
  answer: str
  cited_ranks: list[int] = field(default_factory=list)
  cited_result_ids: list[str] = field(default_factory=list)
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
  ranks, ids = parse_citation_ids(response.text, results)
  return AnswerResult(
    question=question,
    answer=response.text,
    cited_ranks=ranks,
    cited_result_ids=ids,
    backend=response.backend,
    model=response.model,
    raw_response=response,
  )
