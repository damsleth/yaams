from __future__ import annotations

from datetime import UTC, datetime

from yaams.retrieve import HybridResult, ScoreComponents
from yaams.synthesize import (
  AnswerResult,
  DummyAdapter,
  SubprocessAdapter,
  build_synthesis_prompt,
  llm_adapter_from_config,
  parse_citation_ids,
  parse_structured_answer,
  synthesize_answer,
)


def _make_result(rid: str, content: str, kind: str = "item") -> HybridResult:
  return HybridResult(
    id=rid,
    kind=kind,
    source="imessage" if kind == "item" else "consolidation_imessage",
    timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    sender="alice@example.test",
    subject="",
    content=content,
    thread_id="t1",
    score=0.5,
    components=ScoreComponents(),
    participants=["alice@example.test", "user@example.test"],
    item_count=1 if kind == "item" else 7,
  )


def test_build_synthesis_prompt_renders_sources_with_indices():
  results = [
    _make_result("r1", "first message"),
    _make_result("r2", "second message"),
  ]
  prompt = build_synthesis_prompt("what happened", results)
  assert "QUESTION:\nwhat happened" in prompt
  assert "[1] imessage" in prompt
  assert "[2] imessage" in prompt
  assert "first message" in prompt
  assert "second message" in prompt


def test_build_synthesis_prompt_handles_empty_results():
  prompt = build_synthesis_prompt("anything", [])
  assert "(no sources retrieved)" in prompt


def test_build_synthesis_prompt_adds_date_lead_for_last_occurrence():
  results = [_make_result("r1", "latest message")]
  prompt = build_synthesis_prompt("when did I last", results, shape="last_occurrence")
  assert "most recent" in prompt
  assert "Open your answer with that date" in prompt


def test_build_synthesis_prompt_adds_date_lead_for_first_occurrence():
  results = [_make_result("r1", "earliest message")]
  prompt = build_synthesis_prompt("when did I first", results, shape="first_occurrence")
  assert "earliest" in prompt
  assert "Open your answer with that date" in prompt


def test_build_synthesis_prompt_no_date_lead_for_factual():
  results = [_make_result("r1", "a message")]
  prompt = build_synthesis_prompt("who said what", results, shape="factual")
  assert "Open your answer with that date" not in prompt


def test_build_synthesis_prompt_truncates_huge_content():
  results = [_make_result("r1", "x" * 5000)]
  prompt = build_synthesis_prompt("q", results)
  assert "..." in prompt
  assert "x" * 5000 not in prompt


def test_parse_citation_ids_maps_to_result_ids():
  results = [
    _make_result("ra", "a"),
    _make_result("rb", "b"),
    _make_result("rc", "c"),
  ]
  ranks, ids = parse_citation_ids("Per [1] and also [3], not [99].", results)
  assert ranks == [1, 3]
  assert ids == ["ra", "rc"]


def test_parse_citation_ids_dedupes_repeated_citations():
  results = [_make_result("ra", "a")]
  ranks, ids = parse_citation_ids("[1] and again [1] and again [1]", results)
  assert ranks == [1]
  assert ids == ["ra"]


def test_dummy_adapter_returns_predictable_response():
  adapter = DummyAdapter(model_name="test-model")
  response = adapter.complete("hello there friend")
  assert response.backend == "dummy"
  assert response.model == "test-model"
  assert "hello there friend" in response.text or "[dummy" in response.text


def test_synthesize_answer_returns_answer_result_with_citations():
  results = [_make_result("ra", "the cat is on the mat")]

  class _CitingAdapter:
    backend_name = "fake"
    model_name = "v1"

    def complete(self, prompt, *, max_tokens=1024, temperature=0.0):
      from yaams.synthesize.llm import LLMResponse
      return LLMResponse(
        text="The cat is on the mat [1].",
        backend=self.backend_name,
        model=self.model_name,
      )

  outcome = synthesize_answer("where is the cat", results, _CitingAdapter())
  assert isinstance(outcome, AnswerResult)
  assert outcome.cited_ranks == [1]
  assert outcome.cited_result_ids == ["ra"]
  assert outcome.backend == "fake"


def test_subprocess_adapter_pipes_prompt_to_command(tmp_path):
  fake = tmp_path / "fake-llm"
  fake.write_text(
    "#!/bin/sh\n"
    "input=$(cat)\n"
    "echo \"echo: ${input}\"\n"
  )
  fake.chmod(0o755)
  adapter = SubprocessAdapter(command=[str(fake)], model_name="echoer")
  response = adapter.complete("hello world")
  assert "hello world" in response.text
  assert response.backend == "subprocess"
  assert response.model == "echoer"


def test_llm_adapter_from_config_defaults_to_dummy():
  adapter = llm_adapter_from_config({})
  assert isinstance(adapter, DummyAdapter)


def test_llm_adapter_from_config_picks_subprocess():
  adapter = llm_adapter_from_config({
    "synth": {
      "backend": "subprocess",
      "command": ["echo", "hi"],
      "model": "test",
    }
  })
  assert isinstance(adapter, SubprocessAdapter)
  assert adapter.command == ["echo", "hi"]


def test_llm_adapter_from_config_subprocess_requires_command():
  import pytest

  with pytest.raises(ValueError):
    llm_adapter_from_config({"synth": {"backend": "subprocess"}})


def test_parse_structured_answer_well_formed():
  text = (
    "ANSWER:\nThe cat is on the mat [1].\n\n"
    "CONFIDENCE: high\nMultiple sources agree.\n\n"
    "GAPS:\n- nothing about the dog"
  )
  body, conf, reason, gaps = parse_structured_answer(text)
  assert "cat is on the mat" in body
  assert "CONFIDENCE" not in body
  assert conf == "high"
  assert "Multiple sources" in reason
  assert gaps == ["nothing about the dog"]


def test_parse_structured_answer_missing_gaps_section():
  text = "ANSWER:\nthe answer [1].\n\nCONFIDENCE: medium\nsome reason"
  body, conf, reason, gaps = parse_structured_answer(text)
  assert "the answer" in body
  assert conf == "medium"
  assert reason == "some reason"
  assert gaps == []


def test_parse_structured_answer_missing_confidence_section():
  text = "ANSWER:\nbody only\n\nGAPS:\n- thing one\n- thing two"
  body, conf, reason, gaps = parse_structured_answer(text)
  assert body.strip() == "body only"
  assert conf == "unknown"
  assert reason == ""
  assert gaps == ["thing one", "thing two"]


def test_parse_structured_answer_no_markers_at_all():
  text = "Just a flat answer with [1] citation, no sections."
  body, conf, reason, gaps = parse_structured_answer(text)
  assert "Just a flat answer" in body
  assert conf == "unknown"
  assert reason == ""
  assert gaps == []


def test_parse_structured_answer_gaps_says_none():
  text = "ANSWER:\nbody\n\nCONFIDENCE: low\nweak\n\nGAPS:\nnone"
  _, _, _, gaps = parse_structured_answer(text)
  assert gaps == []


def test_synthesize_answer_populates_structured_fields():
  from datetime import UTC, datetime

  from yaams.retrieve import HybridResult, ScoreComponents
  from yaams.synthesize.llm import LLMResponse

  result = HybridResult(
    id="ra",
    kind="item",
    source="imessage",
    timestamp=datetime(2026, 4, 1, tzinfo=UTC),
    sender="alice",
    subject="",
    content="hi",
    thread_id="t",
    score=0.5,
    components=ScoreComponents(),
  )

  class _Adapter:
    backend_name = "fake"
    model_name: str | None = "v1"

    def complete(self, prompt, *, max_tokens=1024, temperature=0.0):
      return LLMResponse(
        text=(
          "ANSWER:\nclear [1].\n\n"
          "CONFIDENCE: high\nplenty of evidence\n\n"
          "GAPS:\nnone"
        ),
        backend=self.backend_name,
        model=self.model_name,
      )

  outcome = synthesize_answer("q", [result], _Adapter())
  assert outcome.confidence == "high"
  assert outcome.confidence_reason == "plenty of evidence"
  assert outcome.gaps == []
  assert "clear" in outcome.answer_body
  assert outcome.cited_ranks == [1]
