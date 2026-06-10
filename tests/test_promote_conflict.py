"""Plan 40 Phase E: LLM conflict classification tests.

All tests use stub adapters — no real model or network access.

Coverage:
  E1  strip_private_fences, _build_prompt, _parse_json_response, classify_pair
  E2  generate_candidates integration (conflict fields on PromotionCandidate)
  E3  store_candidates inserts all conflict columns
  E4  write_to_inbox routes "contradict" to _conflicts/, format_note conflict block
  E6  All 16 tests in the plan
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yaams.promote.conflict import (
    CONFLICT_PROMPT_VERSION,
    ConflictConfig,
    ConflictVerdict,
    _build_prompt,
    _parse_json_response,
    classify_pair,
    strip_private_fences,
)
from yaams.promote.review import format_note, write_to_inbox
from yaams.synthesize.llm import LLMResponse

# ---------------------------------------------------------------------------
# Stub adapters
# ---------------------------------------------------------------------------


class _FixedAdapter:
    """Returns a fixed JSON classification string."""

    backend_name = "stub"

    def __init__(self, classification: str = "supplement", confidence: float = 0.9):
        self._classification = classification
        self._confidence = confidence
        self.model_name = "stub-model"

    def complete(self, prompt: str, *, max_tokens: int = 200, temperature: float = 0.1):
        payload = {
            "classification": self._classification,
            "confidence": self._confidence,
            "reason": f"stub reason for {self._classification}",
        }
        return LLMResponse(
            text=json.dumps(payload),
            backend="stub",
            model="stub-model",
        )


class _ExplodingAdapter:
    """Always raises — simulates adapter failure."""

    backend_name = "exploding"
    model_name = None

    def complete(self, prompt: str, **kwargs):
        raise RuntimeError("adapter failure")


class _FencedAdapter:
    """Returns JSON wrapped in markdown code fences."""

    backend_name = "fenced"
    model_name = "fenced-model"

    def complete(self, prompt: str, **kwargs):
        payload = json.dumps({
            "classification": "contradict",
            "confidence": 0.85,
            "reason": "the candidate contradicts the existing note",
        })
        return LLMResponse(
            text=f"```json\n{payload}\n```",
            backend="fenced",
            model="fenced-model",
        )


class _GarbageAdapter:
    """Returns non-JSON garbage."""

    backend_name = "garbage"
    model_name = None

    def complete(self, prompt: str, **kwargs):
        return LLMResponse(text="nope not json", backend="garbage", model=None)


# ---------------------------------------------------------------------------
# E1a: strip_private_fences
# ---------------------------------------------------------------------------


def test_strip_private_fences_removes_span():
    text = "before <private>secret canary</private> after"
    result = strip_private_fences(text)
    assert "secret canary" not in result
    assert "before" in result
    assert "after" in result


def test_strip_private_fences_case_insensitive():
    text = "<PRIVATE>hidden</PRIVATE> visible"
    result = strip_private_fences(text)
    assert "hidden" not in result
    assert "visible" in result


def test_strip_private_fences_multiline():
    text = "start\n<private>\nline1\nline2\n</private>\nend"
    result = strip_private_fences(text)
    assert "line1" not in result
    assert "line2" not in result
    assert "start" in result
    assert "end" in result


def test_strip_private_fences_no_span_unchanged():
    text = "nothing to strip here"
    assert strip_private_fences(text) == text


# ---------------------------------------------------------------------------
# E1b: _build_prompt — canary in <private> must NOT reach the prompt
# ---------------------------------------------------------------------------


def test_build_prompt_does_not_contain_private_content():
    """The canary text inside <private> must never appear in the built prompt."""
    existing_stmt = "The company had revenue of <private>$1.2B confidential</private> last year."
    candidate_stmt = "Revenue was <private>classified amount</private>."
    # build_prompt is called with already-stripped text; verify the callers strip first
    stripped_existing = strip_private_fences(existing_stmt)
    stripped_candidate = strip_private_fences(candidate_stmt)
    prompt = _build_prompt("Note A", stripped_existing, "Draft B", stripped_candidate)
    assert "$1.2B confidential" not in prompt
    assert "classified amount" not in prompt
    assert "EXISTING NOTE" in prompt
    assert "CANDIDATE" in prompt


# ---------------------------------------------------------------------------
# E1c: _parse_json_response
# ---------------------------------------------------------------------------


def test_parse_json_valid():
    text = '{"classification": "supplement", "confidence": 0.9, "reason": "adds date"}'
    parsed = _parse_json_response(text)
    assert parsed["classification"] == "supplement"
    assert parsed["confidence"] == pytest.approx(0.9)
    assert parsed["reason"] == "adds date"


def test_parse_json_fenced():
    payload = '{"classification": "contradict", "confidence": 0.8, "reason": "contradicts"}'
    text = f"```json\n{payload}\n```"
    parsed = _parse_json_response(text)
    assert parsed["classification"] == "contradict"


def test_parse_json_unknown_enum():
    text = '{"classification": "totally-unknown", "confidence": 0.9, "reason": "x"}'
    parsed = _parse_json_response(text)
    assert parsed["classification"] == "unclassified"


def test_parse_json_unparseable():
    parsed = _parse_json_response("not json at all")
    assert parsed["classification"] == "unclassified"
    assert parsed["confidence"] == 0.0
    assert "unparseable" in parsed["reason"]


def test_parse_json_missing_keys():
    # Valid JSON but missing all keys
    parsed = _parse_json_response("{}")
    assert parsed["classification"] == "unclassified"


# ---------------------------------------------------------------------------
# E1d: classify_pair outcomes
# ---------------------------------------------------------------------------


def test_classify_pair_supplement():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    adapter = _FixedAdapter("supplement", 0.9)
    verdict = classify_pair("A", "existing statement", "B", "candidate statement", "notes/a.md", adapter, cfg)
    assert verdict.classification == "supplement"
    assert verdict.confidence == pytest.approx(0.9)
    assert verdict.target_path == "notes/a.md"
    assert verdict.prompt_version == CONFLICT_PROMPT_VERSION


def test_classify_pair_contradict():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    adapter = _FixedAdapter("contradict", 0.95)
    verdict = classify_pair("A", "existing", "B", "candidate", "notes/b.md", adapter, cfg)
    assert verdict.classification == "contradict"


def test_classify_pair_unrelated():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    adapter = _FixedAdapter("unrelated", 0.85)
    verdict = classify_pair("A", "existing", "B", "candidate", "notes/c.md", adapter, cfg)
    assert verdict.classification == "unrelated"


def test_classify_pair_duplicate():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    adapter = _FixedAdapter("duplicate", 0.88)
    verdict = classify_pair("A", "existing", "B", "candidate", "notes/d.md", adapter, cfg)
    assert verdict.classification == "duplicate"


def test_classify_pair_low_confidence_demoted_to_unclassified():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    adapter = _FixedAdapter("supplement", 0.5)  # below threshold
    verdict = classify_pair("A", "existing", "B", "candidate", "notes/e.md", adapter, cfg)
    assert verdict.classification == "unclassified"
    assert verdict.confidence == pytest.approx(0.5)  # raw confidence preserved


def test_classify_pair_adapter_exception_returns_unclassified():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    verdict = classify_pair("A", "existing", "B", "candidate", "notes/f.md", _ExplodingAdapter(), cfg)
    assert verdict.classification == "unclassified"
    assert verdict.model is None


def test_classify_pair_fenced_json_parsed():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    verdict = classify_pair("A", "existing", "B", "candidate", "notes/g.md", _FencedAdapter(), cfg)
    assert verdict.classification == "contradict"


def test_classify_pair_garbage_response_unclassified():
    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    verdict = classify_pair("A", "existing", "B", "candidate", "notes/h.md", _GarbageAdapter(), cfg)
    assert verdict.classification == "unclassified"


def test_classify_pair_private_fence_stripped_before_prompt():
    """Canary text inside <private> must NOT reach the adapter."""
    captured_prompts: list[str] = []

    class _CapturingAdapter:
        backend_name = "capturing"
        model_name = "cap"

        def complete(self, prompt: str, **kwargs):
            captured_prompts.append(prompt)
            return LLMResponse(
                text='{"classification": "supplement", "confidence": 0.9, "reason": "ok"}',
                backend="capturing",
                model="cap",
            )

    cfg = ConflictConfig(enabled=True, confidence_threshold=0.7)
    existing_stmt = "Known fact <private>CANARY_PRIVATE_CONTENT</private> here."
    classify_pair("Existing", existing_stmt, "Candidate", "new fact", "path.md", _CapturingAdapter(), cfg)
    assert captured_prompts, "adapter was never called"
    assert "CANARY_PRIVATE_CONTENT" not in captured_prompts[0]


# ---------------------------------------------------------------------------
# E4a: format_note emits full conflict block when conflict_classification set
# ---------------------------------------------------------------------------


def test_format_note_with_conflict_block():
    candidate = {
        "id": "abc123",
        "entity": "TestCorp",
        "draft_type": "fact",
        "draft_title": "TestCorp main product",
        "draft_statement": "TestCorp makes widgets.",
        "draft_body": "## Statement\nTestCorp makes widgets.",
        "draft_tags": ["tech"],
        "source_item_ids": [],
        "merge_with": "notes/02_facts/fact__testcorp.md",
        "dedup_similarity": 0.85,
        "conflict_classification": "supplement",
        "conflict_confidence": 0.91,
        "conflict_reason": "candidate adds date fact",
        "conflict_model": "ollama/llama3.1",
        "conflict_checked_at": "2026-06-10T12:00:00Z",
        "conflict_target_statement_hash": "sha256:abc",
        "conflict_prompt_version": 1,
    }
    note = format_note(candidate)
    assert "conflict_classification: supplement" in note
    assert "conflict_confidence:" in note
    assert "conflict_reason: candidate adds date fact" in note
    assert "conflict_model: ollama/llama3.1" in note
    assert "merge_with: notes/02_facts/fact__testcorp.md" in note


def test_format_note_without_conflict_block():
    candidate = {
        "id": "xyz",
        "entity": "X",
        "draft_type": "fact",
        "draft_title": "X title",
        "draft_statement": "X statement.",
        "draft_body": "## Statement\nX statement.",
        "draft_tags": [],
        "source_item_ids": [],
    }
    note = format_note(candidate)
    assert "conflict_classification" not in note


# ---------------------------------------------------------------------------
# E4b: write_to_inbox routes "contradict" to _conflicts/
# ---------------------------------------------------------------------------


def test_write_to_inbox_contradict_goes_to_conflicts(tmp_path: Path):
    candidate = {
        "id": "c1",
        "entity": "Corp",
        "draft_type": "fact",
        "draft_title": "Contradicting fact",
        "draft_statement": "Corp made a loss.",
        "draft_body": "## Statement\nCorp made a loss.",
        "draft_tags": [],
        "source_item_ids": [],
        "conflict_classification": "contradict",
    }
    dest = write_to_inbox(candidate, tmp_path)
    assert "_conflicts" in str(dest)
    assert dest.parent == tmp_path / "_conflicts"


def test_write_to_inbox_non_contradict_goes_to_inbox_root(tmp_path: Path):
    candidate = {
        "id": "c2",
        "entity": "Corp",
        "draft_type": "fact",
        "draft_title": "Supplementing fact",
        "draft_statement": "Corp expanded.",
        "draft_body": "## Statement\nCorp expanded.",
        "draft_tags": [],
        "source_item_ids": [],
        "conflict_classification": "supplement",
    }
    dest = write_to_inbox(candidate, tmp_path)
    assert dest.parent == tmp_path


def test_write_to_inbox_no_conflict_goes_to_inbox_root(tmp_path: Path):
    candidate = {
        "id": "c3",
        "entity": "Corp",
        "draft_type": "fact",
        "draft_title": "Plain fact",
        "draft_statement": "Corp exists.",
        "draft_body": "## Statement\nCorp exists.",
        "draft_tags": [],
        "source_item_ids": [],
    }
    dest = write_to_inbox(candidate, tmp_path)
    assert dest.parent == tmp_path


# ---------------------------------------------------------------------------
# E3: store_candidates inserts conflict columns
# ---------------------------------------------------------------------------


def _open_db() -> sqlite3.Connection:
    from yaams.schema import init_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn, embedding_dim=4, use_vec=False)
    return conn


def test_store_candidates_conflict_columns_persisted():
    from yaams.promote.candidates import PromotionCandidate, store_candidates

    conn = _open_db()
    c = PromotionCandidate(
        id="test-id-1",
        entity="CorpX",
        draft_type="fact",
        draft_title="Some fact",
        draft_statement="CorpX does something.",
        draft_body="## Statement\nCorpX does something.",
        draft_tags=["tag1"],
        source_item_ids=["item1"],
        merge_with="notes/fact__corpx.md",
        dedup_similarity=0.84,
        conflict_classification="supplement",
        conflict_confidence=0.91,
        conflict_reason="adds data",
        conflict_model="ollama/llama3.1",
        conflict_checked_at="2026-06-10T12:00:00Z",
        conflict_target_statement_hash="sha256:deadbeef",
        conflict_prompt_version=1,
    )
    stored = store_candidates(conn, [c])
    assert stored == 1

    row = conn.execute(
        "SELECT * FROM promotion_candidates WHERE id = ?", (c.id,)
    ).fetchone()
    assert row is not None
    assert row["conflict_classification"] == "supplement"
    assert row["conflict_confidence"] == pytest.approx(0.91)
    assert row["conflict_reason"] == "adds data"
    assert row["conflict_model"] == "ollama/llama3.1"
    assert row["conflict_checked_at"] == "2026-06-10T12:00:00Z"
    assert row["conflict_target_statement_hash"] == "sha256:deadbeef"
    assert row["conflict_prompt_version"] == 1
    assert row["merge_with"] == "notes/fact__corpx.md"
    assert row["dedup_similarity"] == pytest.approx(0.84)


def test_store_candidates_null_conflict_columns():
    """A candidate with no conflict fields stores NULLs without error."""
    from yaams.promote.candidates import PromotionCandidate, store_candidates

    conn = _open_db()
    c = PromotionCandidate(
        id="test-id-2",
        entity="CorpY",
        draft_type="fact",
        draft_title="Another fact",
        draft_statement="CorpY exists.",
        draft_body="## Statement\nCorpY exists.",
        draft_tags=[],
        source_item_ids=[],
    )
    stored = store_candidates(conn, [c])
    assert stored == 1
    row = conn.execute(
        "SELECT conflict_classification FROM promotion_candidates WHERE id = ?", (c.id,)
    ).fetchone()
    assert row["conflict_classification"] is None
