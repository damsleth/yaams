"""Golden-text tests for _render_result multi-line layout (Plan: no prior coverage).

Pins the exact TTY output produced by _render_result for:
- a standard (non-consolidation) result with multi-line body text
- a consolidation result with participants
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import call, patch

from yaams.cli.query import _render_result


def _fake_result(
    *,
    kind: str = "email",
    source: str = "email_crayon",
    timestamp_str: str = "2026-03-15 09:30 UTC",
    score: float = 0.875,
    sender: str = "alice@example.com",
    content: str = "Hello world",
    participants: list[str] | None = None,
    item_count: int = 1,
) -> SimpleNamespace:
    """Build a fake HybridResult-shaped object for render tests.

    We use a plain datetime string as the timestamp so that format_local
    (which converts to local tz) is bypassed — the ``hasattr(r.timestamp,
    'strftime')`` branch in _render_result returns False for a plain str, so
    the header line uses str(r.timestamp) directly.  This makes the golden
    string timezone-independent.
    """
    return SimpleNamespace(
        kind=kind,
        source=source,
        timestamp=timestamp_str,
        score=score,
        sender=sender,
        content=content,
        participants=participants or [],
        item_count=item_count,
    )


# ---------------------------------------------------------------------------
# Standard (non-consolidation) result — multi-line body
# ---------------------------------------------------------------------------

def test_render_result_standard_multiline_golden():
    """_render_result emits the expected multi-line layout for a normal result."""
    body = (
        "This is the first line of the body.\n"
        "This is the second line of the body.\n"
        "And a third line to confirm multi-line wrapping works correctly."
    )
    r = _fake_result(
        source="email_crayon",
        timestamp_str="2026-03-15 09:30 UTC",
        score=0.875,
        sender="alice@example.com",
        content=body,
    )

    with patch("click.echo") as mock_echo:
        _render_result(3, r)

    calls = [c.args[0] if c.args else "" for c in mock_echo.call_args_list]

    # Golden: header line
    assert calls[0] == "[ 3] email_crayon · 2026-03-15 09:30 UTC · score 0.875"
    # Sender line
    assert calls[1] == "     from alice"
    # Body lines (one per source line, each indented)
    assert calls[2] == "     This is the first line of the body."
    assert calls[3] == "     This is the second line of the body."
    assert calls[4] == "     And a third line to confirm multi-line wrapping works correctly."
    # Trailing blank echo()
    assert calls[5] == ""
    assert len(calls) == 6


# ---------------------------------------------------------------------------
# Consolidation result
# ---------------------------------------------------------------------------

def test_render_result_consolidation_golden():
    """_render_result emits the expected layout for a consolidation result."""
    content = (
        "teams_crayon session 2026-03-15 with alice@example.com, bob@example.com:\n"
        "[2026-03-15 09:00] alice@example.com: Let's sync up\n"
        "[2026-03-15 09:01] bob@example.com: Sounds good"
    )
    r = _fake_result(
        kind="consolidation",
        source="teams_crayon",
        timestamp_str="2026-03-15 09:00 UTC",
        score=0.920,
        participants=["alice@example.com", "bob@example.com"],
        item_count=2,
        content=content,
    )

    with patch("click.echo") as mock_echo:
        _render_result(1, r)

    calls = [c.args[0] if c.args else "" for c in mock_echo.call_args_list]

    # Header
    assert calls[0] == "[ 1] teams_crayon · 2026-03-15 09:00 UTC · score 0.920"
    # Meta: item_count · participants
    assert calls[1] == "     2 items · alice, bob"
    # Body lines from render_consolidation_snippet (header stripped, emails shortened)
    assert calls[2] == "     09:00 alice: Let's sync up"
    assert calls[3] == "     09:01 bob: Sounds good"
    # Trailing blank
    assert calls[4] == ""
    assert len(calls) == 5


# ---------------------------------------------------------------------------
# Long body wraps at _BODY_WIDTH = 92
# ---------------------------------------------------------------------------

def test_render_result_long_line_wraps():
    """Lines exceeding _BODY_WIDTH (92) are wrapped with subsequent_indent."""
    long_line = "word " * 25  # 125 chars — must wrap
    r = _fake_result(content=long_line.strip())

    with patch("click.echo") as mock_echo:
        _render_result(1, r)

    calls = [c.args[0] if c.args else "" for c in mock_echo.call_args_list]
    # Skip header (calls[0]) and sender (calls[1]); body starts at calls[2]
    # There must be more than one body echo call for a 125-char line
    body_calls = calls[2:-1]  # exclude trailing blank
    assert len(body_calls) > 1, "expected wrapping to produce multiple lines"
    # Each body piece starts with the standard indent or the wrap indent
    for piece in body_calls:
        assert piece.startswith("     ") or piece.startswith("     " + "  "), (
            f"unexpected indent in wrapped piece: {piece!r}"
        )
