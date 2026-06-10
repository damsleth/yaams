"""Snapshot tests for render_card_lines (pure TTY renderer, no curses)."""

from yaams.signals.review import ReviewItem, ReviewResult, render_card_lines


def _make_item(text: str = "nocos standup decisions", shape: str = "factual") -> ReviewItem:
    results = [
        ReviewResult(
            rank=i,
            result_id=f"r{i}",
            kind="item",
            source="teams",
            rrf_score=1.0 / i,
            snippet=f"Snippet for result {i}. " + ("Detail here. " * 4),
            sender="Alice" if i == 1 else None,
            timestamp=f"2026-05-0{i}T09:00:00",
            cited=(i == 1),
        )
        for i in range(1, 4)
    ]
    return ReviewItem(
        query_id="q1",
        text=text,
        ts="2026-06-10T08:00:00",
        results_returned=3,
        shape=shape,
        confidence="high",
        cited_count=1,
        results=results,
        reasons=["low hit rate"],
    )


def test_rank1_expanded_others_collapsed():
    item = _make_item()
    lines = render_card_lines(item, width=80)
    joined = "\n".join(lines)

    # Rank 1 expanded: snippet text is present
    assert "Snippet for result 1" in joined
    # Ranks 2 and 3 collapsed: "(tab to expand)" marker
    assert "(tab to expand)" in joined
    # Query text on first line
    assert lines[0].startswith("Q: nocos standup")


def test_explicit_expanded_ranks():
    item = _make_item()
    lines = render_card_lines(item, expanded_ranks={1, 2}, width=80)
    joined = "\n".join(lines)

    assert "Snippet for result 1" in joined
    assert "Snippet for result 2" in joined
    # Rank 3 still collapsed
    assert "(tab to expand)" in joined


def test_keybar_enter_default_shown():
    item = _make_item(text="nocos standup decisions")
    lines = render_card_lines(item, width=80)
    keybar = lines[-1]
    # default_verdict should fire for a query with tokens in snippet
    assert "enter=" in keybar


def test_cited_star_in_header():
    item = _make_item()
    lines = render_card_lines(item, width=80)
    joined = "\n".join(lines)
    # Rank 1 is cited=True → ★ marker
    assert "★" in joined


def test_golden_text_multiline_layout():
    item = _make_item(text="test query", shape=None)
    item.parser_fallback = True
    item.reasons = ["test"]
    # Single short snippet so the golden is stable
    item.results = [
        ReviewResult(
            rank=1,
            result_id="r1",
            kind="item",
            source="mail",
            rrf_score=0.9,
            snippet="Short snippet.",
            sender=None,
            timestamp="2026-01-01T00:00:00",
            cited=False,
        )
    ]
    item.results_returned = 1
    item.cited_count = 0
    item.confidence = None

    lines = render_card_lines(item, width=60)

    assert lines[0] == "Q: test query"
    assert "shape unparsed" in lines[1]
    assert "results 1" in lines[1]
    assert "Short snippet." in "\n".join(lines)
